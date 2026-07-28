from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from uuid import UUID

from ..auth import require_key
from ..db import pool
from ..cronmatch import validate

router = APIRouter(prefix="/apps", tags=["crons"],
                   dependencies=[Depends(require_key)])

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

_SCHEDULE_DOC = (
    "Standard 5-field cron expression in UTC: 'minute hour day-of-month month "
    "day-of-week', where day-of-week is 0-6 with 0 = Sunday. Supports *, ranges "
    "(1-5), lists (1,3,5) and steps (*/15). Named macros such as @daily and day "
    "names such as MON are NOT supported and will be rejected. Examples: "
    "'*/10 * * * *' every ten minutes, '0 6 * * *' at 06:00 UTC daily, "
    "'0 9 * * 1' at 09:00 UTC on Mondays."
)


class CronIn(BaseModel):
    schedule: str = Field(description=_SCHEDULE_DOC)
    method: Method = Field(default="GET", description="HTTP method for the call.")
    path: str = Field(
        default="/",
        description="Path appended to the app's base URL, e.g. '/jobs/digest'. "
                    "Must start with '/'.")
    body: str | None = Field(
        default=None,
        description="Request body, sent with Content-Type: application/json. "
                    "Only meaningful for POST, PUT and PATCH.")
    enabled: bool = Field(
        default=True,
        description="Set false to keep the schedule defined but stop it running.")


class CronPatch(BaseModel):
    schedule: str | None = Field(default=None, description=_SCHEDULE_DOC)
    method: Method | None = None
    path: str | None = None
    body: str | None = None
    enabled: bool | None = Field(
        default=None,
        description="Set false to pause this job without deleting it.")


async def _require_app(app_id: str) -> None:
    async with pool().acquire() as c:
        if not await c.fetchval("SELECT 1 FROM apps WHERE id = $1", app_id):
            raise HTTPException(404, "no such app")


@router.get("/{app_id}/crons", operation_id="crons_list",
            summary="List an app's scheduled HTTP jobs")
async def list_crons(app_id: str):
    """Every scheduled job attached to an app, with its cron expression, method,
    path and whether it is currently enabled.

    Each job's id is needed to update or delete it, so list first when the user
    describes a job in words rather than by id.
    """
    await _require_app(app_id)
    async with pool().acquire() as c:
        rows = await c.fetch(
            "SELECT id, schedule, method, path, body, enabled, created_at "
            "FROM crons WHERE app_id = $1 ORDER BY created_at", app_id)
    return [dict(r) for r in rows]


@router.post("/{app_id}/crons", status_code=201, operation_id="crons_create",
             summary="Schedule a recurring HTTP call to an app")
async def create_cron(app_id: str, body: CronIn):
    """Schedule a recurring HTTP request to one of the app's own endpoints.

    A dispatcher on the host wakes every minute, works out which jobs are due,
    and calls the app. There is no separate worker or queue — the app itself does
    the work, in whatever handler sits behind the path you give.

    Two useful properties: the call goes through the local proxy rather than out
    to the internet, so schedules keep running even if the tunnel is down; and if
    the app is configured to sleep when idle, a scheduled call wakes it.

    Times are UTC, not the user's local timezone. If they ask for '9am', confirm
    which they mean before converting.
    """
    await _require_app(app_id)
    if not validate(body.schedule):
        raise HTTPException(422, "invalid 5-field cron expression")
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "INSERT INTO crons (app_id, schedule, method, path, body, enabled) "
            "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
            app_id, body.schedule, body.method, body.path, body.body, body.enabled)
    return dict(row)


@router.put("/{app_id}/crons/{cron_id}", operation_id="crons_update",
            summary="Change a scheduled job's timing, target or enabled state")
async def update_cron(app_id: str, cron_id: UUID, body: CronPatch):
    """Modify an existing scheduled job. Send only the fields you want changed;
    anything omitted keeps its current value.

    Setting enabled=false pauses a job while keeping its definition, which is
    usually what the user wants rather than deleting it. Changes take effect
    within a minute, with no redeploy.
    """
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


@router.delete("/{app_id}/crons/{cron_id}", status_code=204,
               operation_id="crons_delete",
               summary="Remove a scheduled job")
async def delete_cron(app_id: str, cron_id: UUID):
    """Delete a scheduled job permanently.

    If the user only wants to stop it temporarily, update it with enabled=false
    instead — that keeps the schedule and target so it can be resumed later.
    """
    async with pool().acquire() as c:
        res = await c.execute(
            "DELETE FROM crons WHERE id = $1 AND app_id = $2", cron_id, app_id)
    if res.endswith("0"):
        raise HTTPException(404, "no such cron")
