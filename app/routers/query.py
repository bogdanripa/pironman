from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from .. import provision

router = APIRouter(prefix="/apps", tags=["database"],
                   dependencies=[Depends(require_key)])


class Script(BaseModel):
    script: str = Field(
        description="For a postgres app: raw SQL, multiple statements allowed, "
                    "semicolon-separated. For a mongo app: a mongosh script, e.g. "
                    "db.notes.find({}).limit(5). Runs as the app's own database "
                    "user against its own database — no other app's data is "
                    "reachable.")
    timeout: int = Field(
        default=30,
        description="Statement timeout in seconds. Raise it for long migrations "
                    "or large index builds.")


@router.post("/{app_id}/db/query", operation_id="run_db_script",
             summary="Run raw SQL or a mongosh script against one app's database")
async def run_query(app_id: str, body: Script):
    """Execute a script directly against an app's database and return the raw
    output.

    Use it to inspect data, apply a schema migration, seed initial rows, or
    repair a bad record — anything you would otherwise do with a psql or mongosh
    session.

    **There are no guardrails.** A DROP TABLE, a DELETE without a WHERE clause,
    or a dropDatabase() will execute exactly as written. Read the script back to
    the user and confirm before running anything that modifies or removes data.
    Reads are safe to run directly.

    Output is the raw stdout of psql or mongosh, formatted as a text table rather
    than JSON — parse it loosely, and prefer explicit column lists over SELECT *
    when you need to read specific values.

    Fails with 400 if the app has no database attached.
    """
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
