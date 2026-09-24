"""Dropping a database must never leave the registry still claiming it.

Run it directly: `python tests/test_detach_db.py` (needs the app's dependencies;
nothing here touches Docker, Coolify or the database).

On 2026-09-24 `apps_detach_db pingpong` answered HTTP 500 and stranded the app.
The order was: drop the database, call Coolify, then clear the row. `pingpong`
was a bare registration with no container, so `coolify_uuid` was empty, and
`GET /applications//envs` answers `{"message":"Not found."}` — a 404, which
`_request` turns into a CoolifyError. The exception skipped the row update.

What that leaves is worse than a failed call, because the destructive half had
already succeeded. The database and its role were gone, while the row still read
`db_engine=postgres`, so:

  * apps_get composed a DATABASE_URL pointing at a database that no longer
    existed, and reported db_size_mb as null;
  * apps_attach_db refused with 409 "app already has a postgres database";
  * apps_detach_db itself could not be retried — it 400s on "app has no
    database" only when db_engine is NULL, so the retry path ran the same
    failing call for ever.

And it was invisible: the MCP layer invokes the route in-process rather than
over the network, so uvicorn never logged the exception. The only trace on the
box was one line, `ERROR:fastapi_mcp.server:Error calling apps_detach_db`.

So the guard alone is not the fix. The ordering is: once the drop has happened
it is irreversible, and the row has to stop naming the database before anything
that can fail runs. These cases pin both.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for k in ("COOLIFY_TOKEN", "COOLIFY_PROJECT", "COOLIFY_SERVER",
          "COOLIFY_DESTINATION", "PAAS_DB_PASSWORD"):
    os.environ.setdefault(k, "x")
os.environ.setdefault("PAAS_DB_HOST", "localhost")

from fastapi import HTTPException  # noqa: E402

from app import coolify  # noqa: E402
from app.routers import apps  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class FakeConn:
    """Records every statement, and serves the one row the route reads."""

    def __init__(self, world):
        self.world = world

    async def fetchrow(self, sql, *args):
        return self.world["row"]

    async def execute(self, sql, *args):
        self.world["calls"].append(("sql", sql.split()[0] + " " + sql.split()[1]))
        if sql.startswith("UPDATE apps SET db_engine=NULL"):
            self.world["row_cleared"] = True


class FakePool:
    def __init__(self, world):
        self.world = world

    def acquire(self):
        world = self.world

        class _Ctx:
            async def __aenter__(self):
                return FakeConn(world)

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def world_with(uuid, db_engine="postgres", coolify_raises=False):
    """A fake box. `row` is what the apps table holds; `calls` is the order in
    which the route touched the outside world, which is the thing under test."""
    world = {
        "row": {"coolify_uuid": uuid, "db_engine": db_engine},
        "calls": [],
        "row_cleared": False,
        "dropped": False,
    }

    async def drop(app_id, engine):
        world["calls"].append(("drop", app_id))
        world["dropped"] = True

    async def delete_env(u, key):
        world["calls"].append(("coolify.delete_env", u))
        if coolify_raises:
            raise coolify.CoolifyError("GET /applications//envs -> 404: Not found.")

    async def sync_env(conn, u, app_id, *a):
        world["calls"].append(("envs.sync_env", u))

    async def deploy(u, **kw):
        world["calls"].append(("coolify.deploy", u))

    fake = FakePool(world)
    apps.pool = lambda: fake      # the route calls pool(), not pool
    apps.provision.drop = drop
    apps.coolify.delete_env = delete_env
    apps.coolify.deploy = deploy
    apps.envs.sync_env = sync_env
    return world


def run(app_id="pingpong"):
    try:
        return asyncio.run(apps.detach_db(app_id)), None
    except HTTPException as e:
        return None, e


def main():
    print("[an app that was registered but never deployed — the pingpong case]")
    world = world_with(uuid="")
    result, exc = run()
    check("no coolify_uuid -> no Coolify call at all",
          not any(c[0].startswith("coolify") for c in world["calls"]),
          str(world["calls"]))
    check("the database is still dropped", world["dropped"])
    check("the row is cleared", world["row_cleared"])
    check("it succeeds rather than 500ing", exc is None and result is not None,
          str(exc.detail if exc else ""))
    check("the result says there was no container",
          result is not None and result.get("container") is None)

    print("\n[a Coolify failure must not strand the row — the actual defect]")
    world = world_with(uuid="abc123", coolify_raises=True)
    result, exc = run()
    check("the row is cleared EVEN THOUGH Coolify failed", world["row_cleared"])
    check("the failure is reported, not swallowed", exc is not None)
    check("it is not a bare 500", exc is not None and exc.status_code == 502,
          str(exc.status_code if exc else ""))
    check("the message says the drop already happened",
          exc is not None and "dropped" in exc.detail)
    check("the message says what is left to do",
          exc is not None and "apps_env_delete" in exc.detail)

    print("\n[ordering: the row stops naming the database before anything risky]")
    world = world_with(uuid="abc123")
    run()
    kinds = [c[0] for c in world["calls"]]
    upd = next(i for i, k in enumerate(kinds) if k == "sql")
    first_coolify = next(i for i, k in enumerate(kinds) if k.startswith("coolify"))
    check("drop comes first", kinds[0] == "drop", str(kinds))
    check("the UPDATE comes before any Coolify call", upd < first_coolify, str(kinds))

    print("\n[the ordinary path still does the whole job]")
    world = world_with(uuid="abc123")
    result, exc = run()
    check("no error", exc is None)
    check("row cleared", world["row_cleared"])
    for what in ("coolify.delete_env", "envs.sync_env", "coolify.deploy"):
        check(f"{what} ran", any(c[0] == what for c in world["calls"]))

    print("\n[an app with no database is still refused]")
    world = world_with(uuid="abc123", db_engine=None)
    result, exc = run()
    check("400, not a drop", exc is not None and exc.status_code == 400)
    check("nothing was dropped", not world["dropped"])
    check("nothing was written", not world["row_cleared"])

    print("\n[an app that does not exist]")
    world = world_with(uuid="abc123")
    world["row"] = None
    result, exc = run()
    check("404", exc is not None and exc.status_code == 404)

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main())
