from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Literal
from uuid import UUID

from ..auth import require_key
from ..db import pool
from ..cronmatch import validate

router = APIRouter(prefix="/apps", tags=["crons"],
                   dependencies=[Depends(require_key)])

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class CronIn(BaseModel):
    schedule: str
    method: Method = "GET"
    path: str = "/"
    body: str | None = None
    enabled: bool = True


class CronPatch(BaseModel):
    schedule: str | None = None
    method: Method | None = None
    path: str | None = None
    body: str | None = None
    enabled: bool | None = None


async def _require_app(app_id: str) -> None:
    async with pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")


@router.get("/{app_id}/crons")
async def list_crons(app_id: str):
    await _require_app(app_id)
    async with pool().acquire() as c:
        rows = await c.fetch(
            "SELECT id, schedule, method, path, body, enabled, created_at "
            "FROM crons WHERE app_id = $1 ORDER BY created_at", app_id)
    return [dict(r) for r in rows]


@router.post("/{app_id}/crons", status_code=201)
async def create_cron(app_id: str, body: CronIn):
    await _require_app(app_id)
    if not validate(body.schedule):
        raise HTTPException(422, "invalid 5-field cron expression")
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "INSERT INTO crons (app_id, schedule, method, path, body, enabled) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
            app_id, body.schedule, body.method, body.path, body.body, body.enabled)
    return dict(row)


@router.put("/{app_id}/crons/{cron_id}")
async def update_cron(app_id: str, cron_id: UUID, body: CronPatch):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "nothing to update")
    if "schedule" in fields and not validate(fields["schedule"]):
        raise HTTPException(422, "invalid 5-field cron expression")

    sets = ", ".join(f"{k} = ${i+3}" for i, k in enumerate(fields))
    async with pool().acquire() as c:
        row = await c.fetchrow(
            f"UPDATE crons SET {sets} WHERE id = $1 AND app_id = $2 RETURNING *",
            cron_id, app_id, *fields.values())
    if not row:
        raise HTTPException(404, "no such cron")
    return dict(row)


@router.delete("/{app_id}/crons/{cron_id}", status_code=204)
async def delete_cron(app_id: str, cron_id: UUID):
    async with pool().acquire() as c:
        res = await c.execute(
            "DELETE FROM crons WHERE id = $1 AND app_id = $2", cron_id, app_id)
    if res.endswith("0"):
        raise HTTPException(404, "no such cron")
