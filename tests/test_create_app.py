"""Creating an app: the key must not leak, and a pair must be all-or-nothing.

Run it directly: `python tests/test_create_app.py`.

Two changes are under test and they fail in opposite directions.

`github_repo` exists because a tool result is a transcript — read by people who
were not in the conversation, summarised into other conversations, stored by
systems nobody audited. `paas_key` in a result is a published credential, and
"shown once" describes the mint, not the transcript. So when a repository is
named the key goes straight to GitHub's secrets API and must not appear in the
result at all, including in the error path: a failure to install a key is not a
reason to publish it.

`staging` creates two apps in one call, which introduces a state that did not
exist before — half a pair. That is worse than no pair: the missing half's id
is free for something else to take, while the half that exists looks deliberate.
So every failure after the first app is registered must roll BOTH back.
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

from app import github_api  # noqa: E402
from app.routers import apps  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class FakeConn:
    def __init__(self, world):
        self.world = world

    async def fetchval(self, sql, *a):
        if "FROM apps WHERE id" in sql:
            return 1 if a[0] in self.world["existing"] else None
        return None

    async def fetch(self, sql, *a):
        ids = a[0] if a else []
        return [{"id": i, "db_user": i, "db_password": f"pw-{i}", "db_name": i}
                for i in ids if i in self.world["rows"]]

    async def fetchrow(self, sql, *a):
        return None

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO apps"):
            self.world["rows"].add(a[0])
        elif sql.startswith("DELETE FROM apps"):
            self.world["rows"].discard(a[0])
        self.world["sql"].append(sql.split("\n")[0][:40])


def install(world):
    class _Pool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(_s):
                    return FakeConn(world)

                async def __aexit__(_s, *a):
                    return False
            return _Ctx()

    pool = _Pool()
    apps.pool = lambda: pool

    async def create(app_id, engine):
        world["dbs"].add(app_id)
        return {"user": app_id, "password": f"pw-{app_id}", "database": app_id}

    async def drop(app_id, engine):
        world["dbs"].discard(app_id)

    async def compose_url(engine, user, pw, name):
        return f"postgresql://{user}:{pw}@host:5432/{name}"

    async def mint(conn, label, app_id=None):
        return f"KEY-FOR-{app_id}"

    async def set_secret(owner, repo, name, value):
        if world.get("github_raises"):
            raise github_api.GitHubError(
                f"PUT /repos/{owner}/{repo}/actions/secrets/{name} -> 404: x")
        world["secrets"][name] = value

    apps.provision.create = create
    apps.provision.drop = drop
    apps.provision.compose_url = compose_url
    apps.mint_key = mint
    apps.github_api.set_secret = set_secret
    return world


def world_new(**kw):
    w = {"existing": set(), "rows": set(), "dbs": set(), "secrets": {},
         "sql": [], **kw}
    return install(w)


def run(**kw):
    kw.setdefault("db_engine", "none")
    kw.setdefault("health_path", "/")
    kw.setdefault("spa", False)
    kw.setdefault("staging", False)
    kw.setdefault("github_repo", None)
    kw.setdefault("secret_name", None)
    body = apps.CreateApp(**kw)
    try:
        return asyncio.run(apps.create_app(body)), None
    except HTTPException as e:
        return None, e


def blob(x):
    return repr(x)


def main():
    print("[no repository: the key comes back, and says so]")
    w = world_new()
    out, exc = run(id="notes")
    check("created", exc is None and out is not None, str(exc and exc.detail))
    check("paas_key is returned", out and out.get("paas_key") == "KEY-FOR-notes")
    note = (out or {}).get("paas_key_note") or ""
    check("with a warning not to echo it", "Do not echo" in note, note[:90])
    check("and it points at the safer path", "github_repo" in note, note[:90])
    check("no installed-marker when nothing was installed",
          out is not None and "paas_key_installed" not in out)
    check("no secret was installed", not w["secrets"])

    print("\n[github_repo: the key is installed and NEVER in the result]")
    w = world_new()
    out, exc = run(id="notes", github_repo="bogdanripa/notes")
    check("created", exc is None, str(exc and exc.detail))
    check("installed as PAAS_KEY", w["secrets"].get("PAAS_KEY") == "KEY-FOR-notes")
    check("paas_key is null in the result", out and out.get("paas_key") is None)
    check("the key string appears NOWHERE in the result",
          "KEY-FOR-notes" not in blob(out), blob(out)[:200])
    check("it reports where it went",
          out and out["paas_key_installed"] == {"repo": "bogdanripa/notes",
                                                "secret": "PAAS_KEY"})

    print("\n[a custom secret name]")
    w = world_new()
    run(id="notes", github_repo="o/r", secret_name="deploy_token")
    check("uppercased", "DEPLOY_TOKEN" in w["secrets"], str(list(w["secrets"])))

    print("\n[github failure: still no key in the error]")
    w = world_new(github_raises=True)
    out, exc = run(id="notes", github_repo="o/r")
    check("it fails loudly", exc is not None)
    check("the key is not in the error message",
          exc is not None and "KEY-FOR-notes" not in exc.detail, str(exc and exc.detail))
    check("the error says how to retry",
          exc is not None and "apps_deploy_key" in exc.detail)
    check("and the app was rolled back, not left half-made",
          not w["rows"], str(w["rows"]))

    print("\n[staging: two apps, two databases, two keys]")
    w = world_new()
    out, exc = run(id="notes", db_engine="postgres", staging=True,
                   github_repo="bogdanripa/notes")
    check("created", exc is None, str(exc and exc.detail))
    check("both rows exist", w["rows"] == {"notes", "notes-dev"}, str(w["rows"]))
    check("both databases exist", w["dbs"] == {"notes", "notes-dev"}, str(w["dbs"]))
    check("PAAS_KEY is the main app's",
          w["secrets"].get("PAAS_KEY") == "KEY-FOR-notes")
    check("PAAS_KEY_DEV is the SISTER's, not a copy",
          w["secrets"].get("PAAS_KEY_DEV") == "KEY-FOR-notes-dev")
    check("neither key appears in the result",
          "KEY-FOR-notes" not in blob(out), blob(out)[:160])
    check("the sister's url is reported",
          out and out["staging"]["url"].startswith("https://notes-dev-"))
    check("it points at apps_deploy_workflow with dev_app",
          out and "dev_app='notes-dev'" in out["staging"]["next"])

    print("\n[staging: the database password is masked in both halves]")
    check("main db_url is masked",
          out and "pw-notes" not in out.get("db_url", ""), str(out.get("db_url")))
    check("sister db_url is masked",
          out and "pw-notes-dev" not in out["staging"].get("db_url", ""),
          str(out["staging"].get("db_url")))
    check("but it is still recognisably a URL",
          out and out["db_url"].startswith("postgresql://notes:")
          and out["db_url"].endswith("@host:5432/notes"), str(out.get("db_url")))
    check("and it says where the real one is",
          out and "DATABASE_URL" in out["db_password_note"])

    print("\n[a half-made pair is never left behind]")
    w = world_new(github_raises=True)
    out, exc = run(id="notes", db_engine="postgres", staging=True,
                   github_repo="o/r")
    check("it fails", exc is not None)
    check("NO app row survives", not w["rows"], str(w["rows"]))
    check("NO database survives", not w["dbs"], str(w["dbs"]))

    print("\n[the sister id must fit]")
    w = world_new()
    out, exc = run(id="a" * 28, staging=True)
    check("a base id too long to take -dev is refused",
          exc is not None and exc.status_code == 422, str(exc and exc.status_code))
    check("and nothing was created", not w["rows"], str(w["rows"]))
    w = world_new()
    out, exc = run(id="a" * 27, staging=True)
    check("27 characters still fits", exc is None, str(exc and exc.detail))

    print("\n[an existing sister blocks the pair before anything is made]")
    w = world_new()
    w["existing"].add("notes-dev")
    out, exc = run(id="notes", staging=True)
    check("409", exc is not None and exc.status_code == 409,
          str(exc and exc.status_code))
    check("nothing created", not w["rows"], str(w["rows"]))

    print("\n[db_engine 'none' means no database]")
    w = world_new()
    out, exc = run(id="notes", db_engine="none")
    check("no database provisioned", not w["dbs"])
    check("no db_url in the result", out and "db_url" not in out)

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main())
