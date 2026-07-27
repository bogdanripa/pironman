"""Coolify facade.

Endpoints marked VERIFIED were exercised against the live 4.1.2 instance.
The others are the documented shape but were not tested — if a deploy or env
injection misbehaves, check these first against /docs in a browser.
"""
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


async def set_image(uuid: str, image: str) -> None:
    """UNVERIFIED shape."""
    name, _, tag = image.partition(":")
    await _request("PATCH", f"/applications/{uuid}", json={
        "docker_registry_image_name": name,
        "docker_registry_image_tag": tag or "latest",
    })


async def set_env(uuid: str, key: str, value: str) -> None:
    """UNVERIFIED shape. Tries create, falls back to update on conflict."""
    body = {"key": key, "value": value, "is_preview": False}
    try:
        await _request("POST", f"/applications/{uuid}/envs", json=body)
    except CoolifyError as e:
        if "409" in str(e) or "already" in str(e).lower():
            await _request("PATCH", f"/applications/{uuid}/envs", json=body)
        else:
            raise


async def deploy(uuid: str) -> None:
    """UNVERIFIED shape."""
    await _request("POST", "/deploy", params={"uuid": uuid})
