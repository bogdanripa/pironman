"""Traefik routing for frontend apps.

A frontend-only app has no container of its own, so nothing claims its hostname
and Traefik has nowhere to send it. This module puts that route on the **static
host** instead: for every frontend app, a router on the `web` container matching
that app's Host and pointing at the static host's own service.

The routers are written into `web`'s Coolify custom labels (read-only, like the
Sablier enrollment) so a redeploy of the static host cannot regenerate them away.
They are derived from the database each time, so the label set is a pure function
of which apps have frontends — no drift.

Only apps **without** a backend are routed here for now. An app that has both
already has its own container claiming the hostname, and two routers matching the
same Host is ambiguous; giving those apps their frontend needs the backend's
public router replaced by an internal one (see ARCHITECTURE.md §9b).
"""
from . import coolify, sablier
from .config import DOMAIN_SUFFIX

WEB_APP_ID = "web"
_PREFIX = "traefik.http.routers.fe-"


def _service_of(labels: dict[str, str]) -> str | None:
    """The static host's own Traefik service name, read from the labels Coolify
    generated for it (traefik.http.services.<name>.loadbalancer.server.port)."""
    for k in labels:
        if k.startswith("traefik.http.services.") and k.endswith(
                ".loadbalancer.server.port"):
            return k[len("traefik.http.services."):-len(".loadbalancer.server.port")]
    return None


def _without_generated(labels: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in labels.items() if not k.startswith(_PREFIX)}


def build_labels(current: dict[str, str], app_ids: list[str]) -> dict[str, str]:
    """`current` labels plus one router per frontend app. Returns them unchanged
    if the static host's service name can't be determined (better to leave
    routing alone than to write a broken router set)."""
    service = _service_of(current)
    base = _without_generated(current)
    if not service:
        return base
    for app_id in sorted(app_ids):
        r = f"{_PREFIX}{app_id}"
        base[f"{r}.rule"] = f"Host(`{app_id}{DOMAIN_SUFFIX}`)"
        base[f"{r}.entryPoints"] = "http"
        base[f"{r}.service"] = service
        base[f"{r}.middlewares"] = "gzip"
    return base


async def sync_frontend_routes(conn) -> dict:
    """Make the static host's routers match the frontend apps in the database.

    Redeploys the static host **only when the label set actually changes** — a
    frontend deploy is meant to be a one-second file swap, so it must not restart
    the shared host on every upload.
    """
    web = await conn.fetchrow(
        "SELECT coolify_uuid FROM apps WHERE id = $1", WEB_APP_ID)
    if not web or not web["coolify_uuid"]:
        return {"routed": [], "reason": "no 'web' static host app is set up"}

    rows = await conn.fetch(
        "SELECT id FROM apps WHERE has_frontend = true AND coolify_uuid IS NULL "
        "ORDER BY id")
    app_ids = [r["id"] for r in rows]

    current = await sablier._current_labels(web["coolify_uuid"])
    if not current:
        return {"routed": [], "reason": "static host has no running container yet"}

    desired = build_labels(current, app_ids)
    if desired == current:
        return {"routed": app_ids, "changed": False}

    res = await coolify.set_custom_labels(
        web["coolify_uuid"], [f"{k}={v}" for k, v in desired.items()])
    await coolify.deploy(web["coolify_uuid"])
    return {"routed": app_ids, "changed": True,
            "labels_readonly": res.get("readonly", False)}
