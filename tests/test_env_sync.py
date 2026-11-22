"""One env change must cost a couple of Coolify requests, not two per variable.

Run it directly: `python tests/test_env_sync.py` (needs requirements.txt; the
Coolify here is a fake behind httpx.MockTransport, so nothing leaves the process).

On 2026-11-21 nine `PUT /apps/gepetel/env/<KEY>` calls answered 500 between
16:53 and 16:55Z, every one `POST /applications/<uuid>/envs -> 429`. Coolify
allows 200 API requests a minute (`config/api.php`, `API_RATE_LIMIT`), and each
set re-pushed the app's whole env — POST every key, get a 409 for each that
existed, PATCH it — so one key on a 27-var app was ~54 requests and four sets in
a minute were over the limit. The value was already in `app_env` by then, so the
500 also lied: the write had landed, and the caller was invited to retry into
the same limit.

These cases run the real route, the real sync_env and the real coolify._request
against a fake that counts requests and can start answering 429 after N.
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

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import coolify, envs, events  # noqa: E402
from app.routers import env as env_router  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


UUID = "abc123"


class FakeCoolify:
    """Coolify's env API as measured on 4.7.6: every key stored twice, once as
    production and once as an `is_preview` twin, and the twins may drift."""

    def __init__(self, env, limit=None, retry_after="1", always_429=False):
        self.rows = []
        for k, v in env.items():
            self.rows.append({"uuid": f"p-{k}", "key": k, "value": v, "is_preview": False})
            self.rows.append({"uuid": f"v-{k}", "key": k, "value": v + "-drift",
                              "is_preview": True})
        self.limit, self.retry_after, self.always_429 = limit, retry_after, always_429
        self.log = []          # every request that reached the fake
        self.served = 0        # requests not throttled

    def __call__(self, req: httpx.Request) -> httpx.Response:
        import json
        self.log.append((req.method, req.url.path))
        if self.always_429 or (self.limit is not None and self.served >= self.limit):
            return httpx.Response(429, headers={"retry-after": self.retry_after},
                                  json={"message": "Too Many Attempts."})
        self.served += 1
        path = req.url.path
        if path.endswith(f"/applications/{UUID}/envs"):
            if req.method == "GET":
                return httpx.Response(200, json=self.rows)
            body = json.loads(req.content)
            prod = [r for r in self.rows if r["key"] == body["key"] and not r["is_preview"]]
            if req.method == "POST":
                if prod:
                    return httpx.Response(409, json={"message": "already exists"})
                self.rows.append({"uuid": f"p-{body['key']}", "key": body["key"],
                                  "value": body["value"], "is_preview": False})
                return httpx.Response(201, json={"uuid": f"p-{body['key']}"})
            if req.method == "PATCH":
                prod[0]["value"] = body["value"]
                return httpx.Response(201, json={})
        if path.endswith("/deploy"):
            return httpx.Response(200, json={"deployments": []})
        return httpx.Response(404, json={"message": "Not found."})

    def prod(self):
        return {r["key"]: r["value"] for r in self.rows if not r["is_preview"]}

    def count(self, method=None):
        return sum(1 for m, _ in self.log if method is None or m == method)


class FakeConn:
    def __init__(self, world):
        self.w = world

    async def fetchrow(self, sql, *a):
        return self.w["row"]

    async def fetch(self, sql, *a):
        if "FROM shared_env" in sql:
            return [{"key": k, "value": v} for k, v in self.w["shared"].items()]
        if "FROM app_env" in sql:
            return [{"key": k, "value": v} for k, v in self.w["own"].items()]
        if "FROM apps" in sql and "internal = true" in sql:
            return []
        if "FROM apps" in sql:
            return [self.w["row"]]
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO app_env"):
            self.w["own"][a[1]] = a[2]
        elif sql.startswith("INSERT INTO shared_env"):
            self.w["shared"][a[0]] = a[1]
        return "INSERT 0 1"


class FakePool:
    def __init__(self, world):
        self.w = world

    def acquire(self):
        w = self.w

        class _Ctx:
            async def __aenter__(self):
                return FakeConn(w)

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def install(world, fake):
    env_router.pool = lambda: FakePool(world)
    coolify._client = lambda: httpx.AsyncClient(
        base_url="http://coolify/api/v1", transport=httpx.MockTransport(fake))
    waits = []

    async def no_sleep(s):
        waits.append(s)
    coolify._sleep = no_sleep

    async def no_record(*a, **k):
        pass
    events.record = no_record
    return waits


def gepetel_like():
    """27 variables, as gepetel had on 2026-11-21, all already in Coolify."""
    own = {f"VAR_{i:02d}": f"value-{i}" for i in range(25)}
    shared = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
    world = {"row": {"id": "gepetel", "coolify_uuid": UUID, "has_frontend": True,
                     "db_engine": None, "db_user": None, "db_password": None,
                     "db_name": None},
             "own": dict(own), "shared": dict(shared)}
    return world, {**shared, **own}


def set_one(key, value, redeploy=True):
    body = env_router.EnvValue(value=value, redeploy=redeploy)
    try:
        return asyncio.run(env_router.apps_env_set("gepetel", key, body)), None
    except HTTPException as e:
        return None, e


def main():
    print("[one changed key on a 27-var app — the gepetel case]")
    world, current = gepetel_like()
    fake = FakeCoolify(current)
    install(world, fake)
    result, exc = set_one("VAR_03", "new")
    check("succeeds", exc is None, str(exc and exc.detail))
    check("3 Coolify requests in all: GET envs, PATCH one key, deploy",
          fake.count() == 3, str(fake.log))
    check("no POST for a key that already exists", fake.count("POST") == 1,
          str(fake.log))  # the one POST is /deploy
    check("Coolify now holds the new value", fake.prod()["VAR_03"] == "new")
    check("and every other production value is untouched",
          all(fake.prod()[k] == v for k, v in current.items() if k != "VAR_03"))

    print("\n[a new key is one POST, not POST-409-PATCH]")
    world, current = gepetel_like()
    fake = FakeCoolify(current)
    install(world, fake)
    result, exc = set_one("BRAND_NEW", "x", redeploy=False)
    check("succeeds", exc is None, str(exc and exc.detail))
    check("2 requests: GET + POST", fake.count() == 2, str(fake.log))
    check("the key landed", fake.prod().get("BRAND_NEW") == "x")

    print("\n[setting a value Coolify already has writes nothing]")
    world, current = gepetel_like()
    fake = FakeCoolify(current)
    install(world, fake)
    set_one("VAR_00", "value-0", redeploy=False)
    check("1 request: the GET", fake.count() == 1, str(fake.log))

    print("\n[the preview twins drift; only production rows are compared]")
    # Every twin in the fake differs from the registry. Comparing them would
    # PATCH all 27 keys again on every set.
    world, current = gepetel_like()
    fake = FakeCoolify(current)
    install(world, fake)
    set_one("VAR_00", "value-0", redeploy=False)
    check("no PATCH from a drifted preview twin", fake.count("PATCH") == 0,
          str(fake.log))

    print("\n[a 429 burst slows down instead of failing]")
    world, current = gepetel_like()
    fake = FakeCoolify(current, limit=0, retry_after="7")
    install(world, fake)
    orig = fake.__call__
    state = {"n": 0}

    def flaky(req):
        # two 429s, then the limiter has room again
        state["n"] += 1
        if state["n"] <= 2:
            fake.log.append((req.method, req.url.path))
            return httpx.Response(429, headers={"retry-after": "7"})
        fake.limit = None
        return orig(req)
    waits = install(world, flaky)
    result, exc = set_one("VAR_03", "new", redeploy=False)
    check("succeeds after the limiter frees up", exc is None, str(exc and exc.detail))
    check("waited for Retry-After each time", waits == [7.0, 7.0], str(waits))
    check("value pushed", fake.prod()["VAR_03"] == "new")

    print("\n[Retry-After is capped]")
    world, current = gepetel_like()
    fake = FakeCoolify(current, always_429=True, retry_after="3600")
    waits = install(world, fake)
    result, exc = set_one("VAR_03", "new")
    check("each wait capped at 30s", waits and max(waits) <= 30.0, str(waits))
    check("gives up after a bounded number of retries",
          fake.count() == 1 + coolify._RETRY_429, str(fake.count()))

    print("\n[a limit that never frees up is a 502 that says the value is stored]")
    check("not a bare 500", exc is not None and exc.status_code == 502,
          str(exc and exc.status_code))
    check("says it was stored", exc is not None and "was stored" in exc.detail)
    check("says it is not yet pushed and how it will be",
          exc is not None and "NOT yet pushed to gepetel" in exc.detail
          and "next env change" in exc.detail, exc and exc.detail)
    check("names the 429", exc is not None and "429" in exc.detail)
    check("the registry really does hold it", world["own"]["VAR_03"] == "new")
    check("and no deploy was attempted on a failed push",
          not any(p.endswith("/deploy") for _, p in fake.log))

    print("\n[a failed redeploy after a good push says so]")
    world, current = gepetel_like()
    fake = FakeCoolify(current)

    def deploy_fails(req):
        if req.url.path.endswith("/deploy"):
            fake.log.append((req.method, req.url.path))
            return httpx.Response(500, json={"message": "boom"})
        return fake(req)
    install(world, deploy_fails)
    result, exc = set_one("VAR_03", "new")
    check("502", exc is not None and exc.status_code == 502)
    check("says pushed but redeploy failed",
          exc is not None and "redeploy failed" in exc.detail, exc and exc.detail)
    check("the push itself landed", fake.prod()["VAR_03"] == "new")

    print("\n[a shared env_set keeps going past one app's failure]")
    world, current = gepetel_like()
    fake = FakeCoolify(current, always_429=True)
    install(world, fake)
    body = env_router.EnvValue(value="v", redeploy=False)
    try:
        asyncio.run(env_router.env_set("NEW_SHARED", body))
        exc = None
    except HTTPException as e:
        exc = e
    check("502 naming the unpushed app",
          exc is not None and exc.status_code == 502 and "gepetel" in exc.detail,
          exc and exc.detail)
    check("stored in shared_env", world["shared"].get("NEW_SHARED") == "v")

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main())
