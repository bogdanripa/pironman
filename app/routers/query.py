from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_key
from ..db import pool
from .. import provision

router = APIRouter(prefix="/apps", tags=["database"],
                   dependencies=[Depends(require_key)])


class Script(BaseModel):
    script: str = Field(
        description=(
            "The script to run against this app's own database — as the app's own "
            "user, against its own database, with no other app's data reachable. "
            "The engine is fixed by the app, so send the matching dialect:\n"
            "Postgres → raw SQL. Multiple statements allowed, semicolon-separated; "
            "execution stops at the first error. Prefer explicit column lists over "
            "SELECT *. e.g. \"CREATE TABLE notes (id serial PRIMARY KEY, body "
            "text); INSERT INTO notes (body) VALUES ('hi');\"\n"
            "Mongo → a mongosh script (JavaScript); the app's database is already "
            "the selected `db`. e.g. \"db.notes.insertOne({body: 'hi'}); "
            "db.notes.find().limit(5)\""))
    timeout: int = Field(
        default=30,
        description="Statement timeout in seconds. Raise it for long migrations "
                    "or large index builds.")


@router.post("/{app_id}/db/query", operation_id="db_run_script",
             summary="Run raw SQL or a mongosh script against one app's database")
async def run_query(app_id: str, body: Script):
    """Execute a script directly against an app's database and return the raw
    output. The runner follows the app's engine — psql for a postgres app,
    mongosh for a mongo app — so you send the matching dialect (see the script
    field); you do not choose the runner. Check the app's db_engine first if you
    are unsure which it is.

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
