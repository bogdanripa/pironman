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

    **Use db_read_query instead unless something has to be written.** That one
    connects as a role that cannot write at all, so it is safe to run without
    asking and an agent platform will not gate it. This tool exists for the
    cases that genuinely change data: a migration, a seed, a repair.

    **There are no guardrails here.** A DROP TABLE, a DELETE without a WHERE
    clause, or a dropDatabase() will execute exactly as written. Read the script
    back to the user and confirm before running anything that modifies or
    removes data.

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


class ReadQuery(BaseModel):
    query: str = Field(
        description=(
            "A read-only query against this app's own database. The engine is "
            "fixed by the app, so send the matching dialect:\n"
            "Postgres → SQL. e.g. \"SELECT id, email FROM users ORDER BY "
            "created_at DESC LIMIT 20\". Prefer explicit column lists and a "
            "LIMIT.\n"
            "Mongo → a mongosh expression; the app's database is the selected "
            "`db`. e.g. \"db.users.find({}, {email: 1}).limit(20)\"\n\n"
            "A write will be REFUSED BY THE DATABASE, not by this tool — the "
            "connection is a role with SELECT and nothing else, inside a "
            "read-only transaction. So you do not need to phrase the query "
            "defensively; you cannot damage anything through this tool however "
            "it is written."))
    timeout: int = Field(
        default=30,
        description="Statement timeout in seconds.")


@router.post("/{app_id}/db/read", operation_id="db_read_query",
             summary="Run a READ-ONLY query against one app's database")
async def read_query(app_id: str, body: ReadQuery):
    """Query an app's data without being able to change it.

    Use this by default. `db_run_script` is the one to reach for only when
    something actually has to be written — a migration, a seed, a repair — and
    it is marked destructive precisely so an agent platform can gate it. This
    one is safe to hand to an autonomous caller, so the reading an agent needs
    to do all day long no longer requires the tool that can also drop a table.

    **The restriction is the database's, not this tool's.** The connection uses
    a separate `<app>_ro` login that holds SELECT and nothing else. Nothing here
    parses the SQL, on purpose: a "does it start with SELECT" check reads as a
    guardrail and is not one — `WITH x AS (DELETE FROM t RETURNING *) SELECT *
    FROM x` passes it, a VOLATILE function can write, and there are a dozen
    spellings besides. An engine-enforced privilege does not care how the write
    is spelled; that CTE was tried on this box and came back `permission denied
    for table notes`.

    The role also carries `default_transaction_read_only = on`, but that part is
    defence in depth rather than a second wall: a caller can `SET` it off. The
    grants are what actually hold, and they held against every write tried —
    including after disabling the read-only default.

    A write attempt comes back as the database's own error (`permission denied`,
    or `cannot execute INSERT in a read-only transaction`). That is the system
    working, not a fault to route around — if something genuinely needs writing,
    say so and use db_run_script.

    The read-only login is created the first time this is called for an app, and
    its grants are refreshed on each call, so tables created since remain
    readable.

    Output is the raw stdout of psql or mongosh, formatted as a text table
    rather than JSON — parse it loosely.

    Fails with 400 if the app has no database attached.
    """
    async with pool().acquire() as c:
        row = await c.fetchrow(
            "SELECT db_engine, db_name, db_ro_password FROM apps WHERE id = $1",
            app_id)
    if not row:
        raise HTTPException(404, "no such app")
    if not row["db_engine"]:
        raise HTTPException(400, "app has no database")

    # Created on demand: every app that predates this tool has no read-only
    # login yet, and an app that does have one still gets its grants refreshed,
    # which is what makes tables created since the role was made readable.
    try:
        password = await provision.ensure_readonly(
            row["db_name"], row["db_engine"], row["db_ro_password"])
    except provision.ProvisionError as e:
        raise HTTPException(
            502, f"could not prepare the read-only login for {app_id}: {e}")
    if password != row["db_ro_password"]:
        async with pool().acquire() as c:
            await c.execute("UPDATE apps SET db_ro_password = $1 WHERE id = $2",
                            password, app_id)

    try:
        out = await provision.run_script(
            row["db_engine"], row["db_name"],
            provision.ro_user(row["db_name"]), password,
            body.query, body.timeout)
    except provision.ProvisionError as e:
        raise HTTPException(400, str(e))
    return {"output": out, "ran_as": provision.ro_user(row["db_name"]),
            "note": "read-only connection: SELECT-only grants, inside a "
                    "read-only transaction. A write error here is the database "
                    "refusing, not a bug — use db_run_script if something must "
                    "actually be written."}
