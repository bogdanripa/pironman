"""Coolify facade.

Endpoints marked VERIFIED were exercised against the live 4.1.2 instance.
The others are the documented shape but were not tested — if a deploy or env
injection misbehaves, check these first against /docs in a browser.
"""
import base64
import httpx
from . import events
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


async def create_app(image: str, fqdn: str, app_id: str | None = None) -> str:
    """VERIFIED. Returns the new application UUID.

    `app_id` becomes the application's Coolify name. Without it Coolify names the
    app after the image, so the UI (and the labels it derives) show
    'ghcr.io/bogdanripa/space-invaders' rather than 'space-invaders'.
    """
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
    if app_id:
        body["name"] = app_id
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


async def set_custom_labels(uuid: str, labels: list[str],
                            readonly: bool = True,
                            app_id: str | None = None) -> dict:
    """Replace an app's container labels with `labels` verbatim (base64-encoded,
    newline-joined, as Coolify stores them).

    `readonly` asks Coolify to stop regenerating the label block on deploy, which
    is what makes an added router or middleware survive a redeploy. That flag's
    field name is not stable across Coolify builds — this instance rejects
    `is_container_label_readonly_enabled` with 422 "field is not allowed" — so we
    try the known spellings and fall back to writing the labels alone.

    Writing labels without the flag still works; the labels just aren't protected
    from a future Coolify regeneration. Callers that own their labels (routing,
    Sablier) re-sync from the database, so drift self-heals on the next change.
    Returns {"readonly": bool} so callers can tell which happened.
    """
    # Stamp the app's own id on the container. Coolify names containers
    # <resource-uuid>-<timestamp>, which is unreadable in `docker ps` and changes
    # every deploy, so this is how a container says which app it is without
    # anyone having to resolve a uuid.
    if app_id:
        labels = [l for l in labels if not l.startswith("pironman.app=")]
        labels.append(f"pironman.app={app_id}")

    encoded = base64.b64encode("\n".join(labels).encode()).decode()
    if readonly:
        for field in ("is_container_label_readonly_enabled",
                      "custom_labels_readonly",
                      "is_custom_labels_readonly"):
            try:
                await _request("PATCH", f"/applications/{uuid}",
                               json={"custom_labels": encoded, field: True})
                return {"readonly": True, "field": field}
            except CoolifyError as e:
                if "422" not in str(e) or "not allowed" not in str(e):
                    raise  # a real failure, not an unknown field name
    await _request("PATCH", f"/applications/{uuid}", json={"custom_labels": encoded})
    return {"readonly": False}


async def deploy(uuid: str, *, app_id: str, reason: str) -> None:
    """VERIFIED. POST /deploy?uuid= — confirmed both by hand and by the recursive
    self-deploy, which queued and completed a redeploy of paas-api.

    `app_id` and `reason` are required, and required on purpose. This is the only
    way the platform recycles a container, so it is the one place that can
    guarantee a recycle is never anonymous — a new caller cannot forget to say
    why, because it cannot call this without saying why. Write `reason` for
    someone reading it weeks later with no memory of the code path.

    The record is written first and is best-effort: it must not be able to stop a
    deploy, and a deploy that happened without a row is a worse outcome than a
    row for a deploy that then failed. Before, not after, because the deploy of
    the control plane ends this process — an `await` placed after it may never
    run, which is exactly the case worth recording.
    """
    await events.record(app_id, "deploy", reason, uuid=uuid)
    await _request("POST", "/deploy", params={"uuid": uuid})
