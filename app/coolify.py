"""Coolify facade.

Endpoints marked VERIFIED were exercised against the live 4.1.2 instance.
The others are the documented shape but were not tested — if a deploy or env
injection misbehaves, check these first against /docs in a browser.
"""
import base64
import httpx
from .config import (
    COOLIFY_URL, COOLIFY_TOKEN, COOLIFY_PROJECT, COOLIFY_SERVER,
    COOLIFY_DESTINATION, COOLIFY_ENV_NAME,
)


class CoolifyError(RuntimeError):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{COOLIFY_URL}/api/v1",
        headers={"Authorization": f"Bearer {COOLIFY_TOKEN}"},
        timeout=60.0,
    )


async def _request(method: str, path: str, **kw) -> dict | list | None:
    async with _client() as c:
        r = await c.request(method, path, **kw)
    if r.status_code >= 400:
        raise CoolifyError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return None


async def create_app(image: str, fqdn: str) -> str:
    """VERIFIED. Returns the new application UUID."""
    name, _, tag = image.partition(":")
    body = {
        "project_uuid": COOLIFY_PROJECT,
        "server_uuid": COOLIFY_SERVER,
        "environment_name": COOLIFY_ENV_NAME,
        "destination_uuid": COOLIFY_DESTINATION,
        "docker_registry_image_name": name,
        "docker_registry_image_tag": tag or "latest",
        "ports_exposes": "80",
        "domains": fqdn,
        "instant_deploy": False,
    }
    data = await _request("POST", "/applications/dockerimage", json=body)
    return data["uuid"]


async def delete_app(uuid: str) -> None:
    """VERIFIED. Asynchronous — Coolify replies 'deletion request queued'."""
    await _request("DELETE", f"/applications/{uuid}")


async def get_app(uuid: str) -> dict:
    """VERIFIED. Read one application — exercised by apps_adopt when paas-api
    adopted itself. Raises CoolifyError (404) if the uuid does not exist, which
    is how adopt rejects a dangling uuid."""
    data = await _request("GET", f"/applications/{uuid}")
    return data if isinstance(data, dict) else {}


async def set_image(uuid: str, image: str) -> None:
    """VERIFIED. Exercised by the recursive self-deploy: apps_update_code called
    this to repoint paas-api at a new sha- tag, and the new image came up."""
    name, _, tag = image.partition(":")
    await _request("PATCH", f"/applications/{uuid}", json={
        "docker_registry_image_name": name,
        "docker_registry_image_tag": tag or "latest",
    })


async def set_env(uuid: str, key: str, value: str) -> None:
    """UNVERIFIED shape. Tries create, falls back to update on conflict. Note the
    self-deploy did NOT exercise this — paas-api has no env vars and no database,
    so sync_env's loop was empty. The first real env_set is still the test."""
    body = {"key": key, "value": value, "is_preview": False}
    try:
        await _request("POST", f"/applications/{uuid}/envs", json=body)
    except CoolifyError as e:
        if "409" in str(e) or "already" in str(e).lower():
            await _request("PATCH", f"/applications/{uuid}/envs", json=body)
        else:
            raise


async def list_envs(uuid: str) -> list[dict]:
    """UNVERIFIED shape. Each item carries at least 'uuid', 'key' and 'value'."""
    data = await _request("GET", f"/applications/{uuid}/envs")
    return data if isinstance(data, list) else []


async def delete_env(uuid: str, key: str) -> None:
    """UNVERIFIED shape. Remove a variable by name; a no-op if it is not set.
    Coolify keys deletes by the env's own uuid, so look it up first."""
    for env in await list_envs(uuid):
        if env.get("key") == key:
            await _request("DELETE", f"/applications/{uuid}/envs/{env['uuid']}")
            return


async def set_healthcheck(uuid: str, path: str = "/", port: int = 80) -> None:
    """Enable Coolify's healthcheck, which becomes a docker HEALTHCHECK on the
    container. Sablier needs it to tell 'started' from 'ready' — without one,
    the first request after a wake can hit a container that is still booting."""
    await _request("PATCH", f"/applications/{uuid}", json={
        "health_check_enabled": True,
        "health_check_path": path,
        "health_check_port": str(port),
        "health_check_host": "localhost",
        "health_check_method": "GET",
        "health_check_scheme": "http",
        "health_check_return_code": 200,
        "health_check_interval": 10,
        "health_check_timeout": 5,
        "health_check_retries": 5,
        "health_check_start_period": 10,
    })


async def set_custom_labels(uuid: str, labels: list[str], readonly: bool = True) -> None:
    """Replace an app's container labels with `labels` verbatim (base64-encoded,
    newline-joined, as Coolify stores them). readonly=True flips Coolify's
    'readonly labels' flag so it stops regenerating them on deploy — which is what
    makes an added middleware (e.g. Sablier) survive redeploys instead of being
    wiped. UNVERIFIED shape; field names match Coolify 4.x."""
    encoded = base64.b64encode("\n".join(labels).encode()).decode()
    await _request("PATCH", f"/applications/{uuid}", json={
        "custom_labels": encoded,
        "is_container_label_readonly_enabled": readonly,
    })


async def deploy(uuid: str) -> None:
    """VERIFIED. POST /deploy?uuid= — confirmed both by hand and by the recursive
    self-deploy, which queued and completed a redeploy of paas-api."""
    await _request("POST", "/deploy", params={"uuid": uuid})
