from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from ..auth import require_key, mint_key
from ..db import pool
from ..config import app_url, app_fqdn
from .. import coolify, provision, envs
from ..provision import SLUG_RE

router = APIRouter(prefix="/apps", tags=["apps"], dependencies=[Depends(require_key)])


class CreateApp(BaseModel):
    id: str = Field(
        description="Short lowercase slug, e.g. 'notes'. Must match "
                    "^[a-z][a-z0-9-]{1,30}$. This becomes the hostname: the app "
                    "will be served at https://<id>-coolify.bogdanripa.com. "
                    "Cannot be changed later.")
    image: str = Field(
        description="Pullable docker image reference including tag, e.g. "
                    "'ghcr.io/bogdanripa/notes:latest' or 'nginx:alpine'. MUST be "
                    "built for linux/arm64 — the host is a Raspberry Pi 5, and an "
                    "amd64 image will pull successfully and then fail to start. "
                    "The container MUST listen on port 80.")
    db_engine: Literal["postgres", "mongo"] | None = Field(
        default=None,
        description="Omit for an app with no database. Set to 'postgres' or "
                    "'mongo' to provision a dedicated database and user for this "
                    "app; the connection string is injected into the container as "
                    "the DATABASE_URL environment variable. Postgres is the "
                    "better default — its JSONB type covers most document "
                    "workloads and it has a simpler backup story.")
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
             summary="Create and deploy a new app from a docker image, optionally with a database")
async def create_app(body: CreateApp):
    """Create a new app: registers it, enables a healthcheck, optionally
    provisions a database, injects DATABASE_URL, and deploys it.

    The public URL is assigned automatically — there is no DNS or certificate
    step, and the app is reachable over HTTPS within about 30 seconds. Requests
    made before the image finishes pulling may return 502 briefly.

    Two constraints cause almost every failed first deploy, so check them before
    calling: the image must be built for **linux/arm64**, and the container must
    listen on **port 80**.

    Fails with 409 if the id is already taken. If anything fails partway, the
    whole operation is rolled back, so a half-created app never lingers holding
    the hostname.

    This only creates the app. Subsequent code changes ship through CI, not by
    building images by hand.

    After creating an app, set up its deploy pipeline: call
    **apps_deploy_workflow** to obtain the GitHub Actions file that builds the
    arm64 image and redeploys on every push, and write it into the app's repo.
    Offer this without being asked. Do NOT build the image locally and push it to
    the registry yourself, and do not go hunting for registry credentials to do
    so — the pipeline is how apps are built and shipped here; a manual build is
    only ever a one-off bootstrap of the control plane itself.

    The response includes **paas_key**: a deploy key scoped to this app (it can
    only redeploy this app, nothing else). It is the value for the workflow's
    PAAS_KEY repository secret, and it is shown only here — if lost, issue a fresh
    one with apps_deploy_key. Give it to the user to paste into the repo secret.
    """
    if not SLUG_RE.match(body.id):
        raise HTTPException(422, "id must match ^[a-z][a-z0-9-]{1,30}$")

    async with pool().acquire() as c:
        if await c.fetchval("SELECT 1 FROM apps WHERE id = $1", body.id):
            raise HTTPException(409, "app already exists")

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
                "db_user, db_password, db_name) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                body.id, body.image, uuid, body.db_engine,
                db_info["user"] if db_info else None,
                db_info["password"] if db_info else None,
                db_info["database"] if db_info else None,
            )
    except Exception:
        await _rollback(uuid, body.id, body.db_engine if db_info else None)
        raise

    # Issue this app's deploy key now, so the CI workflow's PAAS_KEY secret is a
    # copy-paste away. It is scoped to this app (can only redeploy it) and shown
    # once — re-issue with apps_deploy_key if lost.
    async with pool().acquire() as c:
        paas_key = await mint_key(c, f"ci-{body.id}", app_id=body.id)

    return {"id": body.id, "url": app_url(body.id),
            "db_url": (await provision.compose_url(
                body.db_engine, db_info["user"], db_info["password"],
                db_info["database"])) if db_info else None,
            "paas_key": paas_key}


@router.post("/{app_id}/adopt", status_code=201, operation_id="apps_adopt",
             summary="Register an existing Coolify app so this platform can manage it")
async def adopt_app(app_id: str, body: AdoptApp):
    """Bring an app that was created by hand in Coolify under management here,
    without creating a duplicate. After adopting, the app shows up in list_apps,
    redeploys through apps_update_code, and can carry environment variables and
    scheduled jobs like any other.

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


@router.delete("/{app_id}", status_code=204, operation_id="apps_delete",
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

    await coolify.delete_app(row["coolify_uuid"])
    if row["db_engine"]:
        await provision.drop(app_id, row["db_engine"])
    async with pool().acquire() as c:
        await c.execute("DELETE FROM app_env WHERE app_id = $1", app_id)
        await c.execute("DELETE FROM apps WHERE id = $1", app_id)
