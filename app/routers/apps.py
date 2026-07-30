from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from ..auth import require_key, mint_key
from ..db import pool
from ..config import app_url, app_fqdn
from .. import coolify, provision, envs, autoupdate, sablier, frontends
from ..provision import SLUG_RE

router = APIRouter(prefix="/apps", tags=["apps"], dependencies=[Depends(require_key)])


class CreateApp(BaseModel):
    id: str = Field(
        description="Short lowercase slug, e.g. 'notes'. Must match "
                    "^[a-z][a-z0-9-]{1,30}$. This becomes the hostname: the app "
                    "will be served at https://<id>-coolify.bogdanripa.com. "
                    "Cannot be changed later.")
    image: str | None = Field(
        default=None,
        description="Pullable docker image reference including tag, e.g. "
                    "'ghcr.io/bogdanripa/notes:latest'. MUST be built for "
                    "linux/arm64 — the host is a Raspberry Pi 5, and an amd64 "
                    "image will pull successfully and then fail to start. The "
                    "container MUST listen on port 80. "
                    "Omit it for a **frontend-only** app (a static site or SPA with "
                    "no server of its own): no container is created, and the app "
                    "serves the bundle its CI uploads. An app can gain a backend "
                    "later with apps_set_image.")
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
                    "return HTTP 200 without authentication. Leave as '/' unless "
                    "the app has a dedicated health endpoint or its root path "
                    "requires a login.")


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
    """List all deployed apps with their image, database engine, public URL and
    number of scheduled jobs.

    Call this first whenever the user refers to an app by name — it confirms the
    app exists and shows how it is configured, which avoids guessing at ids.
    """
    async with pool().acquire() as c:
        rows = await c.fetch(
            "SELECT a.id, a.image, a.db_engine, a.created_at, "
            "  (SELECT count(*) FROM crons WHERE app_id = a.id) AS cron_count "
            "FROM apps a ORDER BY a.id"
        )
    return [
        {**dict(r), "url": app_url(r["id"]), "cron_count": int(r["cron_count"])}
        for r in rows
    ]


@router.get("/{app_id}", operation_id="apps_get",
            summary="Get one app's full configuration, including database credentials")
async def get_app(app_id: str):
    """Full detail for a single app: current image, database engine, a
    ready-to-use database connection string, every scheduled job, and the public
    URL.

    The connection string is composed fresh on each call rather than stored,
    because the database container's hostname changes whenever the database
    resource is rebuilt. Always read it from here rather than caching it.
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

    out = {
        "id": row["id"], "url": app_url(row["id"]), "image": row["image"],
        "db_engine": row["db_engine"], "created_at": row["created_at"],
        "crons": [dict(c) for c in crons],
        "env": [{"key": r["key"], "preview": envs.mask(r["value"])} for r in env],
    }
    if row["db_engine"]:
        out["db_url"] = await provision.compose_url(
            row["db_engine"], row["db_user"], row["db_password"], row["db_name"])
    return out


@router.post("", status_code=201, operation_id="apps_create",
             summary="Create a new app — a backend image, a static frontend, or both")
async def create_app(body: CreateApp):
    """Create a new app: registers it, enables a healthcheck, optionally
    provisions a database, injects DATABASE_URL, and deploys it.

    An app can be a **backend** (pass `image`), a **frontend-only** static site or
    SPA (omit `image` — no container is created and its CI uploads a bundle
    instead), or **both** (create with an image, then have CI also upload a
    frontend; they share the one hostname and requests resolve automatically —
    real files are served, browser navigations get index.html so SPA routes work,
    and everything else goes to the backend). A frontend-only app cannot have a
    database, since there is no container to inject DATABASE_URL into.

    The public URL is assigned automatically — there is no DNS or certificate
    step, and the app is reachable over HTTPS within about 30 seconds. Requests
    made before the image finishes pulling may return 502 briefly.

    Give it your **real** image reference, e.g. ghcr.io/you/<app>:latest, even if
    CI has not pushed it yet — the app registers and holds its hostname, and its
    first real deploy lands when CI pushes and calls /refresh. Do NOT bootstrap
    with a placeholder like nginx:alpine: auto-update follows the tag you register
    here, so it would sit watching the placeholder forever. (If you already did,
    apps_set_image fixes it without recreating.)

    Getting the image right avoids almost every failed first deploy — see
    dockerfile_requirements from apps_deploy_workflow: **linux/arm64**, listen on
    **port 80 bound to `::`** (an IPv4-only 0.0.0.0 bind fails the in-container
    healthcheck), **run as root**, and ship curl for the healthcheck.

    This **queues** a deploy and returns; it does NOT wait for the container to
    become healthy. The rollback-on-failure covers provisioning only — the
    Coolify app, database and env — not the container's runtime: an image that
    starts and then fails its healthcheck stays registered, holding the hostname
    while serving 502. So after creating, **verify with apps_logs** (or by hitting
    the URL). The id-taken case still fails fast with 409.

    This only creates the app. Subsequent code changes ship through CI, not by
    building images by hand.

    After creating an app, set up its deploy pipeline: call
    **apps_deploy_workflow** to obtain the GitHub Actions file that builds the
    arm64 image and redeploys on every push, and write it into the app's repo.
    Offer this without being asked. Do NOT build the image locally and push it to
    the registry yourself, and do not go hunting for registry credentials to do
    so — the pipeline is how apps are built and shipped here; a manual build is
    only ever a one-off bootstrap of the control plane itself.

    The response also includes **paas_key**, a deploy key scoped to this app, for
    the *authenticated* deploy path (PUT /apps/<id>/code) — e.g. a manual
    rollback. The CI workflow does not need it: deploys are secretless, the box
    auto-updates and CI just calls the app's /refresh hook. Shown once; re-issue
    with apps_deploy_key.
    """
    if not SLUG_RE.match(body.id):
        raise HTTPException(422, "id must match ^[a-z][a-z0-9-]{1,30}$")
    if body.db_engine and not body.image:
        raise HTTPException(
            422, "a frontend-only app (no image) cannot have a database — its "
                 "DATABASE_URL would have no container to be injected into")

    async with pool().acquire() as c:
        if await c.fetchval("SELECT 1 FROM apps WHERE id = $1", body.id):
            raise HTTPException(409, "app already exists")

    # Frontend-only: nothing to run, so no Coolify application, no healthcheck and
    # no deploy — just a registered id holding its hostname, ready for its CI to
    # upload a bundle. Its traffic is served by the shared static host.
    if not body.image:
        async with pool().acquire() as c:
            await c.execute(
                "INSERT INTO apps (id, image, coolify_uuid, sleep_when_idle) "
                "VALUES ($1, NULL, NULL, false)", body.id)
            key = await mint_key(c, f"deploy:{body.id}", app_id=body.id)
        return {
            "id": body.id,
            "url": app_url(body.id),
            "kind": "frontend-only",
            "paas_key": key,
            "deploy": [
                "This app has no container — it serves a static bundle from the "
                "shared frontend host.",
                "1. Call apps_deploy_workflow and use its `optional_frontend_job` "
                "as the app's whole workflow (skip the backend `deploy` job).",
                "2. Add the paas_key above as the repo's PAAS_KEY secret — the "
                "frontend upload is authenticated (unlike backend deploys).",
                "3. Push to main. CI builds the static assets, zips them and "
                "uploads them; the site is live in about a second.",
                "Add a backend later with apps_set_image — the two then share this "
                "hostname automatically.",
            ],
        }

    uuid = await coolify.create_app(body.image, app_fqdn(body.id))

    db_info = None
    try:
        await coolify.set_healthcheck(uuid, body.health_path)

        if body.db_engine:
            db_info = await provision.create(body.id, body.db_engine)

        # Inject the app's environment: any shared variables plus, when it has a
        # database, a freshly composed DATABASE_URL. A brand-new app has no
        # app-specific variables yet, so this is shared + DATABASE_URL.
        async with pool().acquire() as c:
            await envs.sync_env(
                c, uuid, body.id, body.db_engine,
                db_info["user"] if db_info else None,
                db_info["password"] if db_info else None,
                db_info["database"] if db_info else None)

        await coolify.deploy(uuid)

        async with pool().acquire() as c:
            await c.execute(
                "INSERT INTO apps (id, image, coolify_uuid, db_engine, "
                "db_user, db_password, db_name, watch_tag) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                body.id, body.image, uuid, body.db_engine,
                db_info["user"] if db_info else None,
                db_info["password"] if db_info else None,
                db_info["database"] if db_info else None,
                # Auto-update on by default, watching whatever tag it was created
                # with (usually :latest) — a push to that tag auto-deploys.
                autoupdate.tag_of(body.image),
            )
    except Exception:
        await _rollback(uuid, body.id, body.db_engine if db_info else None)
        raise

    # Issue this app's deploy key now, so the CI workflow's PAAS_KEY secret is a
    # copy-paste away. It is scoped to this app (can only redeploy it) and shown
    # once — re-issue with apps_deploy_key if lost.
    async with pool().acquire() as c:
        paas_key = await mint_key(c, f"ci-{body.id}", app_id=body.id)

    return {
        "id": body.id,
        "url": app_url(body.id),
        "db_url": (await provision.compose_url(
            body.db_engine, db_info["user"], db_info["password"],
            db_info["database"])) if db_info else None,
        "paas_key": paas_key,
        # A runbook for the caller to drive the rest end to end, without a human
        # and without building anything by hand.
        "deploy": [
            "The app exists but has no code pipeline yet. Set up CI now — no "
            "secret and no human step needed. Do not build an image locally or "
            "look for registry credentials.",
            "1. Call apps_deploy_workflow for this app id to get the GitHub "
            "Actions workflow file and where it goes.",
            "2. Write that workflow verbatim to .github/workflows/deploy.yml in "
            "the app's repo and commit it (use your GitHub tools).",
            "3. Push to main. CI builds and pushes the arm64 image and calls this "
            "app's /refresh hook; the box redeploys the new image. This app "
            "auto-updates, so no PAAS_KEY or deploy secret is required. Deploying "
            "is CI's job — there is no tool to deploy by hand.",
        ],
    }


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


@router.put("/{app_id}/image", operation_id="apps_set_image",
            summary="Point an app at a different base image (and watch tag)")
async def set_app_image(app_id: str, body: SetImage):
    """Change the base image an app runs — e.g. move it off a bootstrap
    placeholder onto its real repository, or switch registries — and redeploy.

    This also repoints auto-update at the new tag and clears the recorded digest,
    so the app tracks the right image from here on. It differs from routine
    deploys, which are automatic (CI push -> /refresh), and from apps_update_code
    (the authenticated CI redeploy path): use this to change *which* image the
    app follows, not to roll out a new build of the same one.

    Called on a **frontend-only** app (one created with no image), this gives it a
    backend for the first time: the container is created and deployed, and from
    then on it serves both — its frontend bundle plus this backend on the same
    hostname.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine, db_user, db_password, db_name, "
            "watch_tag FROM apps WHERE id = $1", app_id)
        if not row:
            raise HTTPException(404, "no such app")

        # Frontend-only app gaining a backend: there is no Coolify application
        # yet, so create one (and its healthcheck) before pointing it anywhere.
        gained_backend = not row["coolify_uuid"]
        if gained_backend:
            uuid = await coolify.create_app(body.image, app_fqdn(app_id))
            try:
                await coolify.set_healthcheck(uuid, "/")
            except Exception:
                pass  # a missing healthcheck must not strand a created app
            await c.execute(
                "UPDATE apps SET coolify_uuid = $1, sleep_when_idle = true "
                "WHERE id = $2", uuid, app_id)
            row = await c.fetchrow(
                "SELECT coolify_uuid, db_engine, db_user, db_password, db_name, "
                "watch_tag FROM apps WHERE id = $1", app_id)

        await coolify.set_image(row["coolify_uuid"], body.image)
        await envs.sync_env(c, row["coolify_uuid"], app_id, row["db_engine"],
                            row["db_user"], row["db_password"], row["db_name"])
        await coolify.deploy(row["coolify_uuid"])
        # Keep auto-update following the new tag (only if it was already on) — and
        # turn it on for a backend that has just been added, matching apps_create.
        watch = (autoupdate.tag_of(body.image)
                 if (gained_backend or row["watch_tag"] is not None) else None)
        await c.execute(
            "UPDATE apps SET image = $1, watch_tag = $2, deployed_digest = NULL "
            "WHERE id = $3", body.image, watch, app_id)
    return {"id": app_id, "image": body.image, "watch_tag": watch}


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
                db_info["database"])}


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
            summary="Turn hourly image auto-update on or off for an app")
async def set_autoupdate(app_id: str, body: AutoUpdate):
    """Enable or disable auto-update for one app.

    On (the default for newly created apps): the platform watches the app's tag
    and redeploys it whenever the registry image changes — hourly, and instantly
    when the app's CI workflow calls its /refresh hook after a push. This is what
    lets a deploy pipeline work with no PAAS_KEY secret.

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
            summary="Turn idle scale-to-zero (Sablier) on or off for an app")
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
        try:
            if body.enabled:
                await sablier.enroll(row["coolify_uuid"], app_id)
            else:
                await sablier.unenroll(row["coolify_uuid"], app_id)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        await c.execute(
            "UPDATE apps SET sleep_when_idle = $1, sablier_enrolled = $1 "
            "WHERE id = $2", body.enabled, app_id)
    return {"id": app_id, "sleep_when_idle": body.enabled}


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
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine, db_user, db_password, db_name "
            "FROM apps WHERE id = $1", app_id)
        if not row:
            raise HTTPException(404, "no such app")

        await coolify.set_image(row["coolify_uuid"], body.image)
        await envs.sync_env(
            c, row["coolify_uuid"], app_id, row["db_engine"],
            row["db_user"], row["db_password"], row["db_name"])

    await coolify.deploy(row["coolify_uuid"])

    async with pool().acquire() as c:
        await c.execute("UPDATE apps SET image = $1 WHERE id = $2",
                        body.image, app_id)
    return {"id": app_id, "image": body.image}


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
        await c.execute("DELETE FROM apps WHERE id = $1", app_id)
    # Return a body, not 204: an MCP tool result with no content is rejected by
    # the connector proxy as "Invalid content from server".
    return {"id": app_id, "deleted": True}
