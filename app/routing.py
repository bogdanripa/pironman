"""Traefik routing for frontend apps.

A frontend-only app has no container of its own, so nothing claims its hostname
and Traefik has nowhere to send it. This module puts that route on the **static
host** instead: for every frontend app, a router on the `web` container matching
that app's Host and pointing at the static host's own service.

The routers are written into `web`'s Coolify custom labels (read-only, like the
Sablier enrollment) so a redeploy of the static host cannot regenerate them away.
They are derived from the database each time, so the label set is a pure function
of which apps have frontends — no drift.

An app that has **both** a frontend and a backend needs one extra step: its own
container already claims the public hostname, and two routers matching the same
Host is ambiguous. So the backend's router is rewritten to match
`<app-id>.internal` instead, freeing the public host for the static host — which
proxies back to that internal name through Traefik (and therefore through
Sablier, so a sleeping backend still wakes). See ARCHITECTURE.md §9b.
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


def internal_host(app_id: str) -> str:
    return f"{app_id}.internal"


def internalized(labels: dict[str, str], app_id: str) -> dict[str, str]:
    """The backend's labels with its public Host rule swapped for the internal
    one. Only the Host(...) term is rewritten — any other condition Coolify put
    in the rule (PathPrefix, etc.) is preserved."""
    public = f"Host(`{app_id}{DOMAIN_SUFFIX}`)"
    internal = f"Host(`{internal_host(app_id)}`)"
    return {k: (v.replace(public, internal)
                if k.startswith("traefik.http.routers.") and k.endswith(".rule")
                else v)
            for k, v in labels.items()}


async def _internalize_backend(app_id: str, uuid: str) -> bool:
    """Move a linked backend off the public hostname. No-op (and no redeploy) if
    it is already internal, so this is safe to call on every frontend deploy."""
    labels = await sablier._current_labels(uuid)
    if not labels:
        return False
    desired = internalized(labels, app_id)
    if desired == labels:
        return False
    await coolify.set_custom_labels(uuid, [f"{k}={v}" for k, v in desired.items()])
    await coolify.deploy(uuid)
    return True


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

    # Every app the static host must see is routed to it: one with a frontend, or
    # one with redirect rules (which the static host applies even when there is no
    # bundle). Apps that also have a backend get their own router moved to
    # <app>.internal first, so the two do not both claim the public hostname. The
    # static host is excluded: it serves its own hostname directly.
    rows = await conn.fetch(
        "SELECT id, coolify_uuid FROM apps "
        "WHERE (has_frontend = true OR jsonb_array_length(redirects) > 0) "
        "AND id <> $1 ORDER BY id", WEB_APP_ID)
    app_ids, internalized_ids = [], []
    for r in rows:
        app_ids.append(r["id"])
        if r["coolify_uuid"]:
            try:
                if await _internalize_backend(r["id"], r["coolify_uuid"]):
                    internalized_ids.append(r["id"])
            except Exception:
                # Leave this app's public router alone rather than half-move it;
                # the next sync retries. Its frontend simply isn't live yet.
                app_ids.remove(r["id"])

    current = await sablier._current_labels(web["coolify_uuid"])
    if not current:
        return {"routed": [], "reason": "static host has no running container yet"}

    desired = build_labels(current, app_ids)
    if desired == current:
        return {"routed": app_ids, "changed": False,
                "internalized": internalized_ids}

    res = await coolify.set_custom_labels(
        web["coolify_uuid"], [f"{k}={v}" for k, v in desired.items()])
    await coolify.deploy(web["coolify_uuid"])
    return {"routed": app_ids, "changed": True,
            "internalized": internalized_ids,
            "labels_readonly": res.get("readonly", False)}
