from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from ..auth import require_key
from ..db import pool
from ..config import app_url, app_fqdn
from .. import coolify, provision
from ..provision import SLUG_RE

router = APIRouter(prefix="/apps", tags=["apps"], dependencies=[Depends(require_key)])


class CreateApp(BaseModel):
    id: str = Field(description="slug; becomes the hostname")
    image: str = Field(description="e.g. ghcr.io/bogdanripa/notes:latest")
    db_engine: Literal["postgres", "mongo"] | None = None
    health_path: str = Field(default="/", description="path the healthcheck hits")


class UpdateCode(BaseModel):
    image: str


@router.get("", operation_id="list_apps", summary="List all deployed apps")
async def list_apps():
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


@router.get("/{app_id}", operation_id="get_app", summary="Get one app with its crons and database URL")
async def get_app(app_id: str):
    async with pool().acquire() as c:
        row = await c.fetchrow("SELECT * FROM apps WHERE id = $1", app_id)
        if not row:
            raise HTTPException(404, "no such app")
        crons = await c.fetch(
            "SELECT id, schedule, method, path, body, enabled FROM crons "
            "WHERE app_id = $1 ORDER BY created_at", app_id)

    out = {
        "id": row["id"], "url": app_url(row["id"]), "image": row["image"],
        "db_engine": row["db_engine"], "created_at": row["created_at"],
        "crons": [dict(c) for c in crons],
    }
    if row["db_engine"]:
        out["db_url"] = await provision.compose_url(
            row["db_engine"], row["db_user"], row["db_password"], row["db_name"])
    return out


@router.post("", status_code=201, operation_id="create_app", summary="Create and deploy a new app from a docker image, optionally with a postgres or mongo database")
async def create_app(body: CreateApp):
    if not SLUG_RE.match(body.id):
        raise HTTPException(422, "id must match ^[a-z][a-z0-9-]{1,30}$")

    async with pool().acquire() as c:
        if await c.fetchval("SELECT 1 FROM apps WHERE id = $1", body.id):
            raise HTTPException(409, "app already exists")

    uuid = await coolify.create_app(body.image, app_fqdn(body.id))

    # Anything that fails from here leaves a Coolify app with no registry row —
    # invisible to this API but still holding the hostname. Always roll back.
    db_info = None
    try:
        await coolify.set_healthcheck(uuid, body.health_path)

        if body.db_engine:
            db_info = await provision.create(body.id, body.db_engine)
            url = await provision.compose_url(
                body.db_engine, db_info["user"], db_info["password"],
                db_info["database"])
            await coolify.set_env(uuid, "DATABASE_URL", url)

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

    return {"id": body.id, "url": app_url(body.id),
            "db_url": (await provision.compose_url(
                body.db_engine, db_info["user"], db_info["password"],
                db_info["database"])) if db_info else None}


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


@router.put("/{app_id}/code", operation_id="update_app_code", summary="Point an app at a new image tag and redeploy it")
async def update_code(app_id: str, body: UpdateCode):
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine, db_user, db_password, db_name "
            "FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")

    await coolify.set_image(row["coolify_uuid"], body.image)

    # Recompose DATABASE_URL on every deploy: the container name may have
    # changed since last time.
    if row["db_engine"]:
        url = await provision.compose_url(
            row["db_engine"], row["db_user"], row["db_password"], row["db_name"])
        await coolify.set_env(row["coolify_uuid"], "DATABASE_URL", url)

    await coolify.deploy(row["coolify_uuid"])

    async with pool().acquire() as c:
        await c.execute("UPDATE apps SET image = $1 WHERE id = $2",
                        body.image, app_id)
    return {"id": app_id, "image": body.image}


@router.delete("/{app_id}", status_code=204, operation_id="delete_app", summary="Delete an app, its database and its crons")
async def delete_app(app_id: str):
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT coolify_uuid, db_engine FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")

    await coolify.delete_app(row["coolify_uuid"])
    if row["db_engine"]:
        await provision.drop(app_id, row["db_engine"])
    async with pool().acquire() as c:
        await c.execute("DELETE FROM apps WHERE id = $1", app_id)
