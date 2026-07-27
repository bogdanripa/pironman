from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_key
from ..db import pool
from .. import provision

router = APIRouter(prefix="/apps", tags=["database"],
                   dependencies=[Depends(require_key)])


class Script(BaseModel):
    script: str
    timeout: int = 30


@router.post("/{app_id}/db/query")
async def run_query(app_id: str, body: Script):
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT db_engine, db_user, db_password, db_name "
            "FROM apps WHERE id = $1", app_id)
    if not row:
        raise HTTPException(404, "no such app")
    if not row["db_engine"]:
        raise HTTPException(400, "app has no database")

    try:
        out = await provision.run_script(
            row["db_engine"], row["db_name"], row["db_user"],
            row["db_password"], body.script, body.timeout)
    except provision.ProvisionError as e:
        raise HTTPException(400, str(e))
    return {"output": out}
