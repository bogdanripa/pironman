"""Traefik routing: the static host as every sleeping app's front door.

Two problems have the same answer.

A frontend-only app has no container of its own, so nothing claims its hostname
and Traefik has nowhere to send it.

An app that **sleeps** has the same problem intermittently, and it is worse.
Traefik's Docker provider only sees running containers, so when Sablier stops one
its router disappears — along with the Sablier middleware that was supposed to
wake it. The only route to the app exists exactly when the app does not need it.
A stopped app therefore stays stopped for ever: requests 404, nothing wakes it,
and because being stopped is also what a healthy idle app looks like, nothing
reports it. That is not a corner case, it is every enrolled app, five minutes
after its last request.

So both get a router on the **static host** (`web`), which never sleeps: a router
on the `web` container matching that app's Host and pointing at the static host's
own service. `web` serves the bundle if there is one and forwards everything else
to the backend, waking it through Sablier's API first if it is stopped — see
web/server.py. Traefik is no longer asked to route to a container that isn't
there.

The routers are written into `web`'s Coolify custom labels (read-only, like the
Sablier enrollment) so a redeploy of the static host cannot regenerate them away.
They are derived from the database each time, so the label set is a pure function
of which apps have frontends — no drift.

An app that has **both** a frontend and a backend needs one extra step: its own
container already claims the public hostname, and two routers matching the same
Host is ambiguous. So the backend's router keeps the public Host and gains one
extra condition — a marker header that only the static host sends. A request from
a browser matches the frontend router; the same request forwarded by the static
host matches the backend router. See ARCHITECTURE.md §9b.

An earlier version renamed the backend's router to `<app-id>.internal` instead.
That worked for plain JSON APIs and quietly broke everything else: Traefik
rewrites `X-Forwarded-Host` on the internal hop, so the backend computed its own
public origin as `https://<app-id>.internal` and emitted that in OAuth discovery
metadata, `WWW-Authenticate` challenges and redirects — none of which resolve.
Keeping the real hostname end-to-end means an app cannot tell it is behind the
static host, which is the only way absolute URLs can be right.
"""
from . import coolify, frontends, sablier
from .config import DOMAIN_SUFFIX, STATIC_HOST_APP
from .locks import ROUTING_LOCK, app_lock

WEB_APP_ID = STATIC_HOST_APP
_PREFIX = "traefik.http.routers.fe-"

# The static host stamps this on requests it forwards to a backend, and the
# backend's router requires it. It is a routing discriminator, not a credential:
# it decides which of two routers on the same hostname wins, and an outside caller
# who sets it reaches the backend directly — exactly what it could already do
# before the app had a frontend.
BACKEND_HEADER = "X-Pironman-Backend"
BACKEND_TOKEN = "1"

# Traefik ranks equal-priority routers by rule length, which would already favour
# the (longer) backend rule. Stating it beats relying on that.
BACKEND_PRIORITY = "100"


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


def _marker_term() -> str:
    return f"Header(`{BACKEND_HEADER}`, `{BACKEND_TOKEN}`)"


def scoped(labels: dict[str, str], app_id: str) -> dict[str, str]:
    """The backend's labels with the marker-header condition added to every router
    rule, so the public hostname is shared with the static host's router rather
    than surrendered.

    Only the Host(...) term is touched — any other condition Coolify put in the
    rule (PathPrefix, etc.) is preserved. Idempotent, and it also migrates a rule
    left on `<app-id>.internal` by the earlier scheme back onto the public host.
    """
    public = f"Host(`{app_id}{DOMAIN_SUFFIX}`)"
    legacy = f"Host(`{app_id}.internal`)"
    marker = _marker_term()
    out: dict[str, str] = {}
    for k, v in labels.items():
        if k.startswith("traefik.http.routers.") and k.endswith(".rule"):
            v = v.replace(legacy, public)
            if marker not in v and public in v:
                v = v.replace(public, f"{public} && {marker}", 1)
            if marker in v:
                out[k[: -len(".rule")] + ".priority"] = BACKEND_PRIORITY
        out[k] = v
    return out


async def apply_backend_labels(app_id: str, uuid: str, *, sleeps: bool,
                               fronted: bool) -> bool:
    """Write everything the platform stamps on a backend's container labels, in
    ONE pass: its Sablier enrollment and the static host's marker condition.
    Returns whether anything changed (and therefore whether it redeployed).

    They cannot be two writes. Each is a read-modify-write against the container's
    **live** labels, and a Coolify deploy is asynchronous — so a second write
    moments after the first still reads the old container and silently reverts
    what the first one did. Enrolling an app and then routing it would leave it
    routed but not sleeping, with nothing to say so.

    `fronted` must be true whenever the static host holds this app's hostname:
    scoping a backend that nothing fronts makes it unreachable, since the marker
    header would never be sent.
    """
    base = await sablier._current_labels(uuid)
    if not base:
        raise sablier.NoContainer(app_id)
    desired = (sablier.enrolled_labels(base, app_id) if sleeps
               else sablier.stripped(base, app_id))
    if fronted:
        desired = scoped(desired, app_id)
    if desired == base:
        return False
    await coolify.set_custom_labels(uuid, [f"{k}={v}" for k, v in desired.items()],
                                    app_id=app_id)
    await coolify.deploy(uuid)
    return True


def is_fronted(row) -> bool:
    """Whether the static host holds this app's hostname — see _FRONTED."""
    return bool(row["has_frontend"] or (row["redirects"] or [])
                or (row["coolify_uuid"] and row["sleep_when_idle"])) \
        and not sablier.excluded(row["id"])


# Apps the static host must front:
#   - it has a bundle to serve;
#   - it has redirect rules, which the static host applies even with no bundle;
#   - it has a backend that sleeps, and so needs something that is awake to hold
#     its route and start it.
_FRONTED = ("SELECT id, coolify_uuid, image, redirects, sleep_when_idle, spa "
            "FROM apps "
            "WHERE id <> $1 AND (has_frontend = true "
            "  OR jsonb_array_length(redirects) > 0 "
            "  OR (coolify_uuid IS NOT NULL AND sleep_when_idle = true)) "
            "ORDER BY id")


async def sync_frontend_routes(conn, scope_backends: bool = True) -> dict:
    """Serialised entry point — the body computes one label set for the shared
    static host from the whole apps table, so two concurrent syncs would each
    write a set the other has already invalidated.

    `scope_backends=False` skips the per-backend label pass. Pass it when the
    caller has just written an app's labels itself: this pass reads the container,
    which still shows the pre-deploy set, and would write it back.
    """
    async with ROUTING_LOCK:
        return await _sync(conn, scope_backends)


async def _sync(conn, scope_backends: bool = True) -> dict:
    """Make the static host's routers match the apps that need fronting.

    Redeploys the static host **only when the label set actually changes** — a
    frontend deploy is meant to be a one-second file swap, so it must not restart
    the shared host on every upload.

    Order matters and is the opposite of the obvious one. The static host's
    routers are written **first**, then each backend is scoped. Scoping a backend
    is what stops it answering its public hostname unconditionally, so doing it
    before the static host has a route for that hostname leaves the app reachable
    by nobody. This way the worst case is the reverse: both routers match, the
    backend's longer rule wins, and the app serves as it did before — degraded,
    not dark. The next sync finishes the job.
    """
    web = await conn.fetchrow(
        "SELECT coolify_uuid FROM apps WHERE id = $1", WEB_APP_ID)
    if not web or not web["coolify_uuid"]:
        return {"routed": [], "reason": f"no '{WEB_APP_ID}' static host app is set up"}

    rows = [r for r in await conn.fetch(_FRONTED, WEB_APP_ID)
            if not sablier.excluded(r["id"])]
    app_ids = [r["id"] for r in rows]

    # The manifest is how the static host knows an app has a backend at all. An
    # app fronted only because it sleeps has no bundle and so no directory, and
    # without one every request to it would 404 before reaching the proxy.
    for r in rows:
        try:
            frontends.write_manifest(r["id"], bool(r["image"]),
                                     list(r["redirects"] or []),
                                     spa=bool(r["spa"]))
        except OSError:
            pass  # the volume will be there on the next sync; routing is unharmed

    current = await sablier._current_labels(web["coolify_uuid"])
    if not current:
        return {"routed": [], "reason": "static host has no running container yet"}

    desired = build_labels(current, app_ids)
    changed = desired != current
    readonly = None
    if changed:
        from . import autoupdate  # lazy: keeps the import graph one-directional
        # The static host's own deploy path (its CI /refresh) holds this lock too,
        # so a routing write cannot land in the middle of one. Always taken while
        # holding ROUTING_LOCK and never the other way round, so the order is
        # fixed and the pair cannot deadlock.
        async with app_lock(WEB_APP_ID):
            before = await autoupdate._container_started_at(web["coolify_uuid"])
            res = await coolify.set_custom_labels(
                web["coolify_uuid"], [f"{k}={v}" for k, v in desired.items()],
                app_id=WEB_APP_ID)
            await coolify.deploy(web["coolify_uuid"])
            readonly = res.get("readonly", False)
            # Wait for the new routers to actually exist before taking any
            # backend off its public hostname. Scoping into a static host that is
            # still restarting is the one ordering that leaves an app answering
            # nowhere.
            landed = await autoupdate.verify_deploy(web["coolify_uuid"], before)
        if not landed.get("verified"):
            return {"routed": app_ids, "changed": True, "scoped": [],
                    "labels_readonly": readonly,
                    "reason": "static host did not come back up; backends left on "
                              "their public hostname"}

    scoped_ids = []
    for r in rows if scope_backends else []:
        if not r["coolify_uuid"]:
            continue
        try:
            if await apply_backend_labels(r["id"], r["coolify_uuid"],
                                          sleeps=bool(r["sleep_when_idle"]),
                                          fronted=True):
                scoped_ids.append(r["id"])
        except Exception:
            pass  # leave its public router alone; the next sync retries

    out = {"routed": app_ids, "changed": changed, "scoped": scoped_ids}
    if readonly is not None:
        out["labels_readonly"] = readonly
    return out
