from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from ..auth import require_key, mint_key
from ..db import pool
from ..locks import app_lock
from ..config import app_url, app_fqdn, CONTROL_PLANE_APP
from .. import coolify, provision, envs, autoupdate, sablier, frontends, routing, stats
from ..provision import SLUG_RE

router = APIRouter(prefix="/apps", tags=["apps"], dependencies=[Depends(require_key)])

# The host in a db_url is a Docker container name on the box, not a routable
# address — it resolves only from inside the app's own network. Said wherever a
# db_url is handed out, because the string looks like an ordinary connection URL
# and reads as one you could point a laptop at.
DB_URL_NOTE = (
    "The hostname in db_url is an internal Docker container name: it resolves "
    "only from inside the app's own network, so this string works from the "
    "deployed container and from nowhere else. It is not reachable from your "
    "machine, from CI, or from a local dev server — there is no exposed port and "
    "no tunnel. To read or write this database from outside, use db_run_script; "
    "to develop locally, run your own database and let DATABASE_URL differ "
    "between environments.")


class CreateApp(BaseModel):
    id: str = Field(
        description="Short lowercase slug, e.g. 'notes'. Must match "
                    "^[a-z][a-z0-9-]{1,30}$. This becomes the hostname: the app "
                    "will be served at https://<id>-coolify.bogdanripa.com. "
                    "Cannot be changed later.")
    db_engine: Literal["postgres", "mongo"] | None = Field(
        default=None,
        description="Whether to provision a dedicated database for this app, and "
                    "which engine. Omit (null) for an app that needs no database. "
                    "There is no default engine — pick from the app's data model, "
                    "do not assume one: 'postgres' for relational data, "
                    "transactions or SQL querying (its JSONB type also handles "
                    "semi-structured data well); 'mongo' for document-oriented "
                    "data with a flexible or evolving schema and nested "
                    "documents. When it is not clear-cut, ask the user which they "
                    "want rather than choosing for them. Whichever is chosen, its "
                    "connection string is injected as the DATABASE_URL "
                    "environment variable.")
    health_path: str = Field(
        default="/",
        description="Path the container healthcheck requests, e.g. '/health'. Must "
                    "return HTTP 200 without authentication. This is the app's ONE "
                    "health path: the platform configures the container's "
                    "healthcheck from it when the container is first created, a "
                    "failing check is what gets a deploy rolled back, and the "
                    "deploy workflow's 'wait for healthy' step requests it. Any "
                    "HEALTHCHECK line in the Dockerfile should name the same path "
                    "— it is only a fallback for the case where that configuration "
                    "did not land, and pointing the two at different paths means "
                    "whichever runs is testing a route you did not mean. Leave as "
                    "'/' unless the app has a dedicated health endpoint or its "
                    "root path requires a login — but an app that also ships a "
                    "frontend should ALWAYS set a backend-owned path such as "
                    "'/api/health', because '/' is answered by the static bundle "
                    "with no container in the path.")
    spa: bool = Field(
        default=False,
        description="Set true only for a single-page app whose client-side router "
                    "owns paths that are not files (React Router, Vue Router and "
                    "the like). Then any unmatched path serves index.html so deep "
                    "links work. Left false, an unmatched path is a 404 — which is "
                    "what it should be for a typo or a dead link, and what lets the "
                    "app ship its own 404.html.")


class UpdateCode(BaseModel):
    image: str = Field(
        description="New docker image reference, e.g. "
                    "'ghcr.io/bogdanripa/notes:sha-a1b2c3d'. Prefer an immutable "
                    "tag over ':latest' — the platform pulls by tag, and pushing a "
                    "new ':latest' does not reliably force a fresh pull. Must be "
                    "linux/arm64.")


class SetImage(BaseModel):
    image: str = Field(
        description="New image reference, e.g. 'ghcr.io/bogdanripa/notes:latest'. "
                    "This becomes the base image the app runs AND the tag "
                    "auto-update follows. Use it to move an app off a bootstrap "
                    "placeholder onto its real image, or to change registries — "
                    "not for routine deploys, which happen automatically on push.")


class AttachDb(BaseModel):
    db_engine: Literal["postgres", "mongo"] = Field(
        description="Which database to provision and attach — 'postgres' or "
                    "'mongo'. Choose by the app's data model (postgres for "
                    "relational/SQL/JSONB, mongo for flexible documents); ask the "
                    "user when it isn't clear-cut. The connection string is "
                    "injected as DATABASE_URL and the app is redeployed.")


class AutoUpdate(BaseModel):
    enabled: bool = Field(
        description="Turn auto-update on or off for this app. When on, the "
                    "platform pulls the app's tag hourly (and on a CI /refresh "
                    "call) and redeploys it whenever the image changes — so a "
                    "push to that tag ships automatically, no deploy key needed. "
                    "Turn it off to pin the app to its current image (e.g. after "
                    "a manual rollback you want to keep).")


class UpdateApp(BaseModel):
    auto_update: bool | None = Field(
        default=None,
        description="Whether the box watches this app's tag and redeploys when the "
                    "image changes. On by default; turn it off to pin an app to "
                    "what it is running now, e.g. to hold a manual rollback.")
    sleep_when_idle: bool | None = Field(
        default=None,
        description="Whether this app scales to zero when idle, waking on the next "
                    "request (which then takes a few seconds). Has no effect on a "
                    "frontend-only app: it has no container to stop.")
    spa: bool | None = Field(
        default=None,
        description="Whether unmatched paths are client-side routes. True serves "
                    "index.html for any path the bundle and backend do not have, so "
                    "a single-page app's deep links work. False (the default) makes "
                    "them a 404, and the bundle's own 404.html is used if it has "
                    "one. Takes effect immediately — no redeploy.")
    health_path: str | None = Field(
        default=None,
        description="Path the container healthcheck requests. Use this to correct "
                    "a health_path chosen at create time — e.g. to move an app "
                    "that also serves a frontend off '/', which the static bundle "
                    "answers without involving the container, onto a path its "
                    "backend actually owns. Must return HTTP 200 without "
                    "authentication. Written to the app's Coolify configuration "
                    "at once; the running container keeps its current check until "
                    "the next deploy.")


class Sleep(BaseModel):
    enabled: bool = Field(
        description="Whether this app should scale to zero when idle. On (the "
                    "default for every app except the control plane): Sablier "
                    "stops the container after it is idle and starts it again on "
                    "the next request. Off: the app stays running always. "
                    "Toggling redeploys the app to apply the label change.")


class AdoptApp(BaseModel):
    coolify_uuid: str = Field(
        description="The Coolify application UUID of an app that already exists in "
                    "Coolify but is not yet managed here. Read it from the Coolify "
                    "app URL: .../application/<uuid>.")
    image: str = Field(
        description="The docker image this app currently runs, e.g. "
                    "'ghcr.io/bogdanripa/paas-api:latest'. Recorded so list_apps "
                    "and redeploys report the right image.")


@router.get("", operation_id="apps_list",
            summary="List every app deployed on the Pi")
async def list_apps():
    """List all deployed apps with their shape, image, database engine, public URL
    and number of scheduled jobs.

    Call this first whenever the user refers to an app by name — it confirms the
    app exists and shows how it is configured, which avoids guessing at ids.
    **kind** is 'backend' (a container), 'frontend' (a static bundle, no
    container) or 'both'; apps_get has the full detail for one app.
    """
    async with pool().acquire() as c:
        rows = await c.fetch(
            "SELECT a.id, a.image, a.db_engine, a.created_at, a.has_frontend, "
            "  (SELECT count(*) FROM crons WHERE app_id = a.id) AS cron_count "
            "FROM apps a ORDER BY a.id"
        )
    out = []
    for r in rows:
        d = dict(r)
        has_frontend = bool(d.pop("has_frontend"))
        out.append({
            **d,
            "kind": ("both" if d["image"] and has_frontend else
                     "frontend" if has_frontend else "backend"),
            "url": app_url(r["id"]),
            "cron_count": int(r["cron_count"]),
        })
    return out


def _sleep_summary(row) -> dict:
    """The two scale-to-zero flags, plus the answer they only imply together.

    `sleep_when_idle` is what the app asked for; `sablier_enrolled` is whether
    the middleware is actually stamped on the container. Sleeping is real only
    when both are true — auto-enrolment is gated (SABLIER_AUTO_ENROLL), so an app
    can sit at requested-but-not-enrolled indefinitely. Read either flag alone and
    you get the wrong answer to the question anyone actually has, which is whether
    the first request after a quiet night is slow.
    """
    wants, enrolled = bool(row["sleep_when_idle"]), bool(row["sablier_enrolled"])
    sleeps = wants and enrolled
    return {
        "sleep_when_idle": row["sleep_when_idle"],
        "sablier_enrolled": row["sablier_enrolled"],
        "sleeps": sleeps,
        "first_request_after_idle": (
            "a few seconds — the container is stopped when idle and this request "
            "wakes it. Turn it off with apps_update sleep_when_idle=false."
            if sleeps else
            "immediate — this app stays running and does not scale to zero."
            + ("" if wants or enrolled else
               " (sleep_when_idle is off.)")
            + (" It is marked sleep_when_idle=true but is not enrolled in the "
               "scale-to-zero middleware, so nothing stops it." if wants and not enrolled
               else "")),
    }


@router.get("/{app_id}", operation_id="apps_get",
            summary="Get one app's full configuration, including database credentials")
async def get_app(app_id: str):
    """Full detail for a single app — read this before changing anything about it.

    **kind** tells you the app's shape: 'backend' (a container), 'frontend' (a
    static bundle, no container) or 'both'. Everything else follows from that:
    `backend` describes the container, its image and how it deploys, and
    `frontend` describes what is actually published on disk (file count, size,
    when it was last published) or is null if the app has no frontend.

    Use it to answer "does this app already have a frontend?", "is it
    auto-updating?", "does it sleep when idle?" and "when was it last used?"
    without guessing or making changes to find out.

    The database connection string is composed fresh on each call rather than
    stored, because the database container's hostname changes whenever the
    database resource is rebuilt. Always read it from here rather than caching it.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT * FROM apps WHERE id = $1", app_id)
        if not row:
            raise HTTPException(404, "no such app")
        crons = await c.fetch(
            "SELECT id, schedule, method, path, body, enabled FROM crons "
            "WHERE app_id = $1 ORDER BY created_at", app_id)
        env = await c.fetch(
            "SELECT key, value FROM app_env WHERE app_id = $1 ORDER BY key", app_id)

    fe = frontends.info(app_id)
    has_backend = bool(row["image"])
    out = {
        "id": row["id"], "url": app_url(row["id"]),
        "kind": ("both" if has_backend and fe else
                 "frontend" if fe else "backend"),
        "image": row["image"],
        "db_engine": row["db_engine"], "created_at": row["created_at"],

        # The container half. None for a frontend-only app, which has no
        # container, no image and nothing to deploy or wake.
        "backend": {
            "image": row["image"],
            "auto_update": row["watch_tag"] is not None,
            "watch_tag": row["watch_tag"],
            "deployed_digest": row["deployed_digest"],
            # The path the container healthcheck requests, and what a rollback is
            # decided on. Echoed so it can be confirmed rather than guessed at;
            # change it with apps_update.
            "health_path": row["health_path"],
            **_sleep_summary(row),
            # Live: running / asleep / stopped, plus current CPU and memory.
            "runtime": await stats.app_runtime(row["coolify_uuid"],
                                               bool(row["sleep_when_idle"])),
        } if has_backend else None,

        # What is actually published on disk, not merely what the flag says.
        "frontend": fe,

        # Ordered redirect rules, first match wins (apps_redirects_list/_set).
        "redirects": list(row["redirects"] or []),

        # Whether a path neither the bundle nor the backend has serves index.html
        # (a client-side route) or 404s. See apps_update.
        "spa": row["spa"],

        # When this app was last requested (any traffic, all time), or null if it
        # has never been seen in the access log. apps_stats reports it for every
        # app at once; it belongs here too, because "is anyone still using this?"
        # is a question about one app as often as about all of them.
        "last_seen": await stats.last_seen_of(app_id),

        "crons": [dict(c) for c in crons],
        "env": [{"key": r["key"], "preview": envs.mask(r["value"])} for r in env],
    }
    if row["db_engine"]:
        out["db_url"] = await provision.compose_url(
            row["db_engine"], row["db_user"], row["db_password"], row["db_name"])
        out["db_url_note"] = DB_URL_NOTE
        try:
            size = await provision.db_size(row["db_engine"], row["db_name"],
                                           row["db_user"], row["db_password"])
            out["db_size_mb"] = round(size / 1048576, 1) if size is not None else None
        except Exception:
            out["db_size_mb"] = None  # never fail the read over a size probe
    return out


@router.post("", status_code=201, operation_id="apps_create",
             summary="Register a new app — it owns a hostname; CI decides what it becomes")
async def create_app(body: CreateApp):
    """Register an app: it takes an id, owns a public HTTPS URL immediately, and
    optionally gets a database. Nothing runs yet.

    **You do not say what the app is made of.** No image, no repository — the
    platform learns that from the app's pipeline, which is the only thing that
    knows it. The app becomes:

    - a **frontend** the first time CI uploads a static bundle,
    - a **backend** the first time CI deploys an image (which creates its
      container), or
    - **both**, if its pipeline does both. They share the one hostname.

    So creating an app is a one-off registration; everything after it flows from
    GitHub. Call apps_deploy_workflow next for the workflow to put in the repo.

    A **database** can be attached now even though nothing is running: the
    connection string is composed and injected as DATABASE_URL on every deploy,
    so it lands the first time code ships. There is no default engine — choose
    from the app's data model, and ask the user when it is not clear-cut.

    The response includes **paas_key**, a deploy key scoped to this app, used by
    both halves of a deploy (the backend's /refresh call and the frontend
    upload). Install it as the repo's PAAS_KEY secret with github_secret_set
    rather than asking the user to paste it. Shown once; re-issue with
    apps_deploy_key.
    """
    if not SLUG_RE.match(body.id):
        raise HTTPException(422, "id must match ^[a-z][a-z0-9-]{1,30}$")

    async with pool().acquire() as c:
        if await c.fetchval("SELECT 1 FROM apps WHERE id = $1", body.id):
            raise HTTPException(409, "app already exists")

    # A database can be provisioned now even though there is no container yet:
    # DATABASE_URL is composed and injected on every deploy, so it lands the first
    # time the app's pipeline ships code.
    db_info = None
    if body.db_engine:
        db_info = await provision.create(body.id, body.db_engine)

    try:
        async with pool().acquire() as c:
            await c.execute(
                "INSERT INTO apps (id, image, coolify_uuid, db_engine, "
                "db_user, db_password, db_name, health_path, sleep_when_idle, "
                "spa) "
                "VALUES ($1, NULL, NULL, $2, $3, $4, $5, $6, false, $7)",
                body.id, body.db_engine,
                db_info["user"] if db_info else None,
                db_info["password"] if db_info else None,
                db_info["database"] if db_info else None,
                body.health_path, body.spa)
            key = await mint_key(c, f"ci-{body.id}", app_id=body.id)
    except Exception:
        if db_info:
            try:
                await provision.drop(body.id, body.db_engine)
            except Exception:
                pass
        raise

    out = {
        "id": body.id,
        "url": app_url(body.id),
        "kind": "registered — no code deployed yet",
        "paas_key": key,
        "health_path": body.health_path,
        # "Is the first request after a quiet night slow?" is a first-class
        # question for anything handed to other people, and the two flags that
        # answer it (sleep_when_idle, sablier_enrolled) only exist once there is a
        # container. Answer it here, where the app is being created.
        "idle_behaviour": (
            "A backend is marked sleep_when_idle when its container is first "
            f"created, so {body.id} will be a candidate for scaling to zero. "
            "Sleeping only actually happens once it is also enrolled in the "
            "scale-to-zero middleware, which is gated platform-wide — apps_get "
            "reports both flags plus a plain `first_request_after_idle` answer, "
            "so check there rather than assuming. When it is sleeping, the first "
            "request after an idle period waits a few seconds for the container "
            "to start; every request after that is normal speed until it goes "
            "idle again. Turn it off with apps_update sleep_when_idle=false for "
            "an app where that first-hit delay is not acceptable. A frontend is "
            "never affected: it has no container and is always warm."),
        "deploy": [
            "The app now owns its hostname. What it becomes is decided by what "
            "its pipeline ships: a frontend if CI uploads a bundle, a backend if "
            "CI deploys an image, or both.",
            "1. Install the paas_key above as the repo's PAAS_KEY secret with "
            "github_secret_set — it authenticates both halves of a deploy and can "
            "only deploy this app.",
            "2. Call apps_deploy_workflow and write what it returns into the "
            "app's repo: the backend job, the frontend job, or both.",
            "3. Push to main. The first deploy creates whatever the app needs — "
            "there is no tool here to deploy by hand.",
        ],
    }
    if db_info:
        out["db_url"] = await provision.compose_url(
            body.db_engine, db_info["user"], db_info["password"],
            db_info["database"])
        out["db_url_note"] = DB_URL_NOTE
    return out


@router.post("/{app_id}/adopt", status_code=201, operation_id="apps_adopt",
             summary="Register an existing Coolify app so this platform can manage it")
async def adopt_app(app_id: str, body: AdoptApp):
    """Bring an app that was created by hand in Coolify under management here,
    without creating a duplicate. After adopting, the app shows up in list_apps,
    redeploys through CI like any other app, and can carry environment variables
    and scheduled jobs.

    This is how paas-api manages itself: the control plane was created in Coolify
    by hand during bootstrap, so it has no registry row and cannot be redeployed
    through its own API until it adopts itself once. Do that, add a deploy
    workflow, and every push to main redeploys it recursively through this very
    endpoint.

    Adopting only writes a registry row — it does not touch the running app, its
    environment or its data. The app is registered without a platform-managed
    database (db_engine null): adopt is for apps that own their storage, such as
    paas-api, which reaches the _paas database through PAAS_DB_* rather than a
    DATABASE_URL this platform injects.
    """
    if not SLUG_RE.match(app_id):
        raise HTTPException(422, "id must match ^[a-z][a-z0-9-]{1,30}$")

    async with pool().acquire() as c:
        if await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(409, "app already registered")

    try:
        await coolify.get_app(body.coolify_uuid)
    except coolify.CoolifyError:
        raise HTTPException(404, "no Coolify application with that uuid")

    async with pool().acquire() as c:
        await c.execute(
            "INSERT INTO apps (id, image, coolify_uuid, db_engine) "
            "VALUES ($1, $2, $3, NULL)",
            app_id, body.image, body.coolify_uuid)

    return {"id": app_id, "url": app_url(app_id), "image": body.image,
            "coolify_uuid": body.coolify_uuid, "db_engine": None}


@router.post("/{app_id}/deploy-key", operation_id="apps_deploy_key",
             summary="Issue (or reissue) this app's scoped deploy key")
async def deploy_key(app_id: str):
    """Mint a fresh deploy key for an app and return it. Use this when the key
    from apps_create was lost, when rotating it, or for an app that predates
    per-app keys (e.g. one brought in with apps_adopt).

    The key is scoped: it can only redeploy this app (PUT /apps/<id>/code), so it
    is safe to store as the app's PAAS_KEY repository secret. Issuing a new one
    **revokes** any previous deploy key for this app, so update the repo secret
    after calling this. Shown once.
    """
    async with pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")
        # One active deploy key per app: drop the old before minting the new.
        await c.execute("DELETE FROM api_keys WHERE app_id = $1", app_id)
        paas_key = await mint_key(c, f"ci-{app_id}", app_id=app_id)
    return {"app_id": app_id, "paas_key": paas_key,
            "note": "Scoped to this app. Set it as the PAAS_KEY repo secret; "
                    "any previous deploy key for this app is now revoked."}


@router.get("/{app_id}/logs", operation_id="apps_logs",
            summary="Container status, health and recent logs for one app")
async def app_logs(app_id: str, tail: int = 200):
    """The app's container state, health, and its last `tail` log lines, read
    straight from the Docker daemon.

    Reach for this whenever a deploy 'succeeded' but the URL serves 502, or a
    container won't stay up — create/redeploy only queue the deploy and do not
    wait for the container to become healthy, so this is how you actually see
    what happened (a crash, an EACCES on port 80, an IPv4-only bind failing the
    healthcheck, a missing env var). `status` of 'no container' means the deploy
    failed or was rolled back.
    """
    tail = max(1, min(tail, 2000))
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT coolify_uuid FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    if not row["coolify_uuid"]:
        return {"id": app_id, "container": None,
                "status": "frontend-only — this app has no container, so there "
                          "are no logs. Its static bundle is served by the shared "
                          "frontend host.",
                "logs": ""}
    return {"id": app_id, **await autoupdate.app_logs(row["coolify_uuid"], tail)}


@router.put("/{app_id}", operation_id="apps_update",
            summary="Update an app: give it a backend, or change how it deploys and sleeps")
async def update_app(app_id: str, body: UpdateApp):
    """Change an app's configuration. Every field is optional; only what you pass
    is touched.

    - **auto_update** — whether the box redeploys when the watched tag moves.
    - **sleep_when_idle** — whether the app scales to zero when idle.
    - **spa** — whether an unmatched path is a client-side route (serve
      index.html) or a 404. Only turn it on for an app with a client-side router;
      it applies at once, with no redeploy.
    - **health_path** — the path the container healthcheck requests, for
      correcting the one chosen at create time.

    To give an app a *frontend*, publish one (apps_frontend_write, or a CI upload)
    — there is nothing to configure here for that.
    """
    async with pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")

    changed: dict = {"id": app_id}
    if body.auto_update is not None:
        changed.update(await set_autoupdate(app_id, AutoUpdate(enabled=body.auto_update)))
    if body.sleep_when_idle is not None:
        changed.update(await set_sleep(app_id, Sleep(enabled=body.sleep_when_idle)))
    if body.spa is not None:
        changed.update(await _set_spa(app_id, body.spa))
    if body.health_path is not None:
        changed.update(await _set_health_path(app_id, body.health_path))
    if len(changed) == 1:
        raise HTTPException(400, "nothing to update — pass at least one field")
    return changed


async def _set_health_path(app_id: str, path: str) -> dict:
    """Record a new health path and push it to Coolify.

    Recording it matters even for an app with no container yet: apply_image reads
    health_path when it creates one. For an app that already has a container,
    Coolify is updated now but the running container keeps the healthcheck it was
    started with, so the change lands with the next deploy — say so rather than
    let a caller read the new value back and assume it is in force.
    """
    if not path.startswith("/"):
        raise HTTPException(422, "health_path must start with '/'")
    async with pool().acquire() as c:
        await c.execute("UPDATE apps SET health_path = $1 WHERE id = $2",
                        path, app_id)
        uuid = await c.fetchval(
            "SELECT coolify_uuid FROM apps WHERE id = $1", app_id)

    out = {"health_path": path}
    if not uuid:
        return {**out, "in_force": "on the first deploy, which creates the "
                                   "container"}
    try:
        await coolify.set_healthcheck(uuid, path)
    except Exception as e:
        # Recorded either way: the next container creation reads it from here.
        return {**out, "in_force": f"recorded, but Coolify rejected the update "
                                   f"({e}) — it will be applied when the "
                                   f"container is next recreated"}
    return {**out, "in_force": "on the next deploy — the running container keeps "
                               "the healthcheck it started with"}


async def _set_spa(app_id: str, enabled: bool) -> dict:
    """Turn client-side routing on or off. Only the manifest the static host reads
    changes, so it takes effect on the next request — nothing is redeployed."""
    async with pool().acquire() as c:
        await c.execute("UPDATE apps SET spa = $1 WHERE id = $2", enabled, app_id)
        row = await c.fetchrow(
            "SELECT image, redirects FROM apps WHERE id = $1", app_id)
    frontends.write_manifest(app_id, bool(row["image"]),
                             list(row["redirects"] or []), spa=enabled)
    return {"spa": enabled}


async def _apply_image(app_id: str, image: str) -> dict:
    """Point an app at `image` and redeploy, via the shared implementation in
    autoupdate. Serialised per app: CI's frontend and backend jobs run at once,
    and overlapping Coolify deploys of one app fight rather than queue."""
    async with app_lock(app_id):
        try:
            return await autoupdate.apply_image(app_id, image)
        except autoupdate.NoSuchApp:
            raise HTTPException(404, "no such app")


@router.post("/{app_id}/db", status_code=201, operation_id="apps_attach_db",
             summary="Provision and attach a database to an app that has none")
async def attach_db(app_id: str, body: AttachDb):
    """Add a database to an existing app that doesn't have one: provisions a
    dedicated database and user, injects DATABASE_URL, and redeploys. Returns the
    connection string.

    Fails with 409 if the app already has a database — detach it first with
    apps_detach_db. The app should read DATABASE_URL from its environment.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    if row["db_engine"]:
        raise HTTPException(
            409, f"app already has a {row['db_engine']} database — "
                 "detach it first with apps_detach_db")
    if not row["coolify_uuid"]:
        raise HTTPException(
            400, "this is a frontend-only app: it has no container for "
                 "DATABASE_URL to be injected into. Give it a backend first with "
                 "apps_set_image.")

    db_info = await provision.create(app_id, body.db_engine)
    async with pool().acquire() as c:
        await c.execute(
            "UPDATE apps SET db_engine=$1, db_user=$2, db_password=$3, "
            "db_name=$4 WHERE id=$5",
            body.db_engine, db_info["user"], db_info["password"],
            db_info["database"], app_id)
        await envs.sync_env(c, row["coolify_uuid"], app_id, body.db_engine,
                            db_info["user"], db_info["password"],
                            db_info["database"])
    await coolify.deploy(row["coolify_uuid"])
    return {"id": app_id, "db_engine": body.db_engine,
            "db_url": await provision.compose_url(
                body.db_engine, db_info["user"], db_info["password"],
                db_info["database"]),
            "db_url_note": DB_URL_NOTE}


@router.delete("/{app_id}/db", operation_id="apps_detach_db",
               summary="Drop and detach an app's database (destroys its data)")
async def detach_db(app_id: str):
    """Remove an app's database: **drops the database and all its data**, clears
    DATABASE_URL, and redeploys.

    This is irreversible with no undo — confirm with the user first and say
    explicitly that the data will be destroyed. Fails with 400 if the app has no
    database. The app keeps running, just without a database.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    if not row["db_engine"]:
        raise HTTPException(400, "app has no database")

    await provision.drop(app_id, row["db_engine"])
    await coolify.delete_env(row["coolify_uuid"], "DATABASE_URL")
    async with pool().acquire() as c:
        await c.execute(
            "UPDATE apps SET db_engine=NULL, db_user=NULL, db_password=NULL, "
            "db_name=NULL WHERE id=$1", app_id)
        await envs.sync_env(c, row["coolify_uuid"], app_id,
                            None, None, None, None)
    await coolify.deploy(row["coolify_uuid"])
    return {"id": app_id, "detached": True}


@router.put("/{app_id}/autoupdate", operation_id="apps_autoupdate",
            include_in_schema=False, summary="Turn hourly image auto-update on or off for an app")
async def set_autoupdate(app_id: str, body: AutoUpdate):
    """Enable or disable auto-update for one app.

    On (the default for newly created apps): the platform watches the app's tag
    and redeploys it whenever the registry image changes — hourly, and instantly
    when the app's CI workflow calls its /refresh hook after a push — so a push
    to that tag ships automatically, with no manual deploy step.

    Off: the app stays on whatever image it currently runs until you deploy it
    explicitly. Use this to hold a manual rollback, or for an app you don't want
    tracking a moving tag.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT image FROM apps WHERE id = $1", app_id)
        if not row:
            raise HTTPException(404, "no such app")
        watch = autoupdate.tag_of(row["image"]) if body.enabled else None
        await c.execute("UPDATE apps SET watch_tag = $1 WHERE id = $2",
                        watch, app_id)
    return {"id": app_id, "autoupdate": body.enabled, "watch_tag": watch}


@router.put("/{app_id}/sleep", operation_id="apps_sablier",
            include_in_schema=False, summary="Turn idle scale-to-zero (Sablier) on or off for an app")
async def set_sleep(app_id: str, body: Sleep):
    """Enable or disable Sablier scale-to-zero for one app.

    On (default for every app): the platform stamps the Sablier middleware into
    the app's Coolify labels (read-only, so a redeploy can't strip it) and
    redeploys once. The app then sleeps after it is idle and wakes on the next
    request. Off: the enrollment is removed and the app runs continuously.

    The control-plane app is excluded and cannot be made to sleep — it runs the
    background loops and must stay up. Enabling requires the app to have a running
    container already (deploy it once first) so its base labels can be read.
    """
    if sablier.excluded(app_id):
        raise HTTPException(
            400, f"{app_id} is the control plane and must never sleep")
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT coolify_uuid FROM apps WHERE id = $1", app_id)
        if not row:
            raise HTTPException(404, "no such app")
        if not row["coolify_uuid"]:
            raise HTTPException(
                400, "this is a frontend-only app: it has no container to sleep. "
                     "Its assets are served by the shared frontend host, which "
                     "stays warm.")
        # Turning sleep ON means the static host must already hold this app's
        # hostname, or the first time it sleeps nothing can wake it. Route first,
        # then write the labels — one write, covering both the enrollment and the
        # marker condition (see routing.apply_backend_labels).
        await c.execute(
            "UPDATE apps SET sleep_when_idle = $1 WHERE id = $2",
            body.enabled, app_id)
        try:
            await routing.sync_frontend_routes(c, scope_backends=False)
            fronted = routing.is_fronted(
                await c.fetchrow(
                    "SELECT id, coolify_uuid, has_frontend, redirects, "
                    "sleep_when_idle FROM apps WHERE id = $1", app_id))
            changed = await routing.apply_backend_labels(
                app_id, row["coolify_uuid"], sleeps=body.enabled, fronted=fronted)
        except sablier.NoContainer:
            raise HTTPException(
                409, "this app has never deployed, so it has no labels to build "
                     "on — deploy it once first")
        await c.execute(
            "UPDATE apps SET sablier_enrolled = $1 WHERE id = $2",
            body.enabled, app_id)
    return {"id": app_id, "sleep_when_idle": body.enabled, "redeployed": changed}


async def _rollback(uuid: str, app_id: str, engine: str | None) -> None:
    try:
        await coolify.delete_app(uuid)
    except Exception:
        pass
    if engine:
        try:
            await provision.drop(app_id, engine)
        except Exception:
            pass


@router.put("/{app_id}/code", operation_id="apps_update_code",
            summary="Point an app at a new image tag and redeploy it")
async def update_code(app_id: str, body: UpdateCode):
    """Deploy new code by pointing an existing app at a new docker image tag.

    This is the endpoint the GitHub Actions pipeline calls after it has built and
    pushed an image. You normally do not call it yourself: to ship a change, push
    to the app's repo and let CI build the image and call this. Call it directly
    only to roll back to an image tag that already exists in the registry — never
    as a way to deploy an image you built and pushed by hand.

    The app keeps its URL, its database, its scheduled jobs and its environment
    variables; the full environment — shared variables, the app's own, and a
    recomposed DATABASE_URL — is re-injected on every deploy, so it stays correct
    even if the database container has been rebuilt since the last deploy.

    Use an immutable tag such as ':sha-a1b2c3d'. Re-pushing ':latest' and
    redeploying does not reliably pull the new image.
    """
    # Creates the container on the first call for an app that has none — an app
    # is registered as a shell and becomes a backend app when its pipeline first
    # deploys to it. Also re-injects the environment and repoints auto-update.
    res = await _apply_image(app_id, body.image)
    result = {"id": app_id, "image": res["image"], "watch_tag": res["watch_tag"]}

    # The control plane cannot verify its own deploy: this request is served by
    # the container being replaced, so waiting would kill the response and fail
    # the caller for a deploy that actually worked.
    if app_id == CONTROL_PLANE_APP:
        return {**result, "verified": None,
                "note": "self-deploy — the control plane cannot observe its own "
                        "replacement; check apps_logs afterwards"}

    check = await autoupdate.settle(app_id, res["coolify_uuid"],
                                    res["started_before"])
    if not check.get("verified"):
        raise HTTPException(502, {"deployed": False, "app": app_id,
                                  "reason": check.get("reason"),
                                  "hint": f"apps_logs {app_id} shows why"})
    return {**result, **check}


@router.delete("/{app_id}", operation_id="apps_delete",
               summary="Permanently delete an app, its database and its schedules")
async def delete_app(app_id: str):
    """Destroy an app completely: the container, its database **including all
    data**, and every scheduled job attached to it.

    This is irreversible and there is no undo. Confirm with the user before
    calling it, and say explicitly that the database will be dropped.

    Deletion of the container is asynchronous, so creating a new app with the
    same id immediately afterwards can collide with the one still shutting down.
    Wait a few seconds if recreating.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")

    if row["coolify_uuid"]:  # NULL for a frontend-only app — nothing to delete
        await coolify.delete_app(row["coolify_uuid"])
    if row["db_engine"]:
        await provision.drop(app_id, row["db_engine"])
    frontends.remove(app_id)  # its static bundle, if any
    async with pool().acquire() as c:
        await c.execute("DELETE FROM app_env WHERE app_id = $1", app_id)
        await c.execute("DELETE FROM api_keys WHERE app_id = $1", app_id)
        # Crons too — this is not housekeeping, it is the difference between a
        # deleted app and a deleted app that keeps making requests. The host
        # dispatcher reads the crons table on its own and never joins apps, so a
        # row left behind here fires every minute for ever, at a hostname whose
        # app no longer exists. Nothing reports it: the app is gone from
        # apps_list, its cron_count with it, and the traffic just accrues against
        # a ghost id in the analytics.
        await c.execute("DELETE FROM crons WHERE app_id = $1", app_id)
        await c.execute("DELETE FROM apps WHERE id = $1", app_id)
        try:  # drop its route from the static host, if it had one
            await routing.sync_frontend_routes(c)
        except Exception:
            pass
    # Return a body, not 204: an MCP tool result with no content is rejected by
    # the connector proxy as "Invalid content from server".
    return {"id": app_id, "deleted": True}
