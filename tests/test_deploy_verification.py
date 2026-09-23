"""A deploy is only a success if the RUNNING container is on the deployed image.

Run it directly: `python tests/test_deploy_verification.py` (needs the app's
dependencies; nothing here touches Docker, Coolify or the database).

Every case below is a shape that actually shipped. bt-gateway spent five weeks
serving a stale build while CI went green on every run, because three things
lined up:

  * `verify_deploy` accepted any replacement that came up healthy, without
    checking WHICH image it came up on — and a Coolify rollback is exactly that;
  * `deployed_digest` was written straight after the deploy call, before
    verification, so a failed deploy recorded the digest it had failed to reach;
  * `updated: False` — the branch that reads that digest back — carried no
    `verified` key, and the refresh hook scored anything without one as a pass.

Any one of them alone is a bad day. Together they make the failure permanent and
self-concealing: the platform writes down that the new image is deployed, then
believes itself for ever, and answers every retry with a green no-op.
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

from app import autoupdate  # noqa: E402

OLD = "sha256:" + "2d" * 32
NEW = "sha256:" + "5f" * 32
DIGEST_NEW = "sha256:" + "44" * 32

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class FakeConn:
    """Records writes so a test can assert that one did NOT happen."""
    def __init__(self):
        self.writes = []

    async def execute(self, q, *a):
        self.writes.append((" ".join(q.split()), a))

    def wrote_digest(self):
        return [a for q, a in self.writes if "deployed_digest" in q]


def app_row(deployed_digest):
    return {"id": "bt-gateway", "coolify_uuid": "uuid1",
            "image": "ghcr.io/o/bt-gateway:latest", "watch_tag": "latest",
            "deployed_digest": deployed_digest, "db_engine": None,
            "db_user": None, "db_password": None, "db_name": None,
            "sleep_when_idle": True, "sablier_enrolled": True, "internal": False}


def patch(monkey: dict):
    """Swap module attributes, returning the originals for restoration."""
    old = {k: getattr(autoupdate, k) for k in monkey}
    for k, v in monkey.items():
        setattr(autoupdate, k, v)
    return old


async def main():
    deployed = []

    async def noop(*a, **k):
        return None

    async def fake_deploy(uuid, **k):
        deployed.append(uuid)

    base = {
        "remote_digest": lambda ref: _async(DIGEST_NEW),
        "_container_started_at": lambda u: _async("2026-01-01T00:00:00Z"),
        "envs": type("E", (), {"sync_env": staticmethod(noop)}),
        "coolify": type("C", (), {"set_image": staticmethod(noop),
                                  "deploy": staticmethod(fake_deploy)}),
        "_maybe_enroll_sablier": lambda c, a: _async(False),
        "sleep_after_deploy": lambda a: _async(None),
    }

    def _async(v):
        async def f(*a, **k):
            return v
        return f()

    globals()["_async"] = _async

    print("\n[verify_deploy: the image the container is ON is the criterion]")
    async def no_sleep(_):
        return None
    real_sleep = asyncio.sleep

    # A container that came back up on the OLD image, healthy — a rollback.
    async def fake_docker(*args, timeout=600):
        if args[0] == "ps":
            return 0, "app-container\n"
        fmt = args[2]
        if "StartedAt" in fmt:
            return 0, "2026-01-02T00:00:00Z"   # later than `before`: replaced
        if "Status" in fmt:
            return 0, "running|healthy"
        if "{{.Image}}" in fmt:
            return 0, OLD                       # ...but on the OLD image
        return 1, ""

    o = patch({"_docker": fake_docker})
    asyncio.sleep = no_sleep
    try:
        r = await autoupdate.verify_deploy("uuid1", "2026-01-01T00:00:00Z",
                                           timeout=1, target=NEW)
        check("a healthy replacement running the OLD image is NOT verified",
              r.get("verified") is False, str(r.get("verified")))
        check("and the reason names the revert",
              "not the" in (r.get("reason") or ""), (r.get("reason") or "")[:70])

        r = await autoupdate.verify_deploy("uuid1", "2026-01-01T00:00:00Z",
                                           timeout=1, target=OLD)
        check("the same container IS verified when that is the target",
              r.get("verified") is True, str(r.get("verified")))
    finally:
        asyncio.sleep = real_sleep
        patch(o)

    print("\n[check_and_update: deployed_digest records fact, not intent]")
    async def verified_false(*a, **k):
        return {"verified": False, "reason": "rolled back"}

    async def verified_true(*a, **k):
        return {"verified": True}

    o = patch({**base, "_image_id": lambda ref: _async(NEW),
               "_running_image_id": lambda u: _async(OLD),
               "verify_deploy": verified_false})
    try:
        c = FakeConn()
        deployed.clear()
        r = await autoupdate.check_and_update(c, app_row(None))
        check("a rolled-back deploy does NOT write deployed_digest",
              c.wrote_digest() == [], str(c.wrote_digest()))
        check("and reports verified False", r.get("verified") is False)
    finally:
        patch(o)

    o = patch({**base, "_image_id": lambda ref: _async(NEW),
               "_running_image_id": lambda u: _async(OLD),
               "verify_deploy": verified_true})
    try:
        c = FakeConn()
        r = await autoupdate.check_and_update(c, app_row(None))
        check("a verified deploy DOES write it", len(c.wrote_digest()) == 1)
        check("with the new digest", DIGEST_NEW in str(c.wrote_digest()))
    finally:
        patch(o)

    print("\n[the poisoned state bt-gateway was stuck in, and the self-heal]")
    # The row claims the new digest is deployed; the container says otherwise.
    o = patch({**base, "_image_id": lambda ref: _async(NEW),
               "_running_image_id": lambda u: _async(OLD),
               "verify_deploy": verified_true})
    try:
        c = FakeConn()
        deployed.clear()
        r = await autoupdate.check_and_update(c, app_row(DIGEST_NEW))
        check("digest matches the row but the container is stale -> DEPLOYS",
              deployed == ["uuid1"] and r.get("updated") is True, str(r.get("updated")))
    finally:
        patch(o)

    # Genuinely current: row and container agree.
    o = patch({**base, "_image_id": lambda ref: _async(NEW),
               "_running_image_id": lambda u: _async(NEW),
               "verify_deploy": verified_true})
    try:
        c = FakeConn()
        deployed.clear()
        r = await autoupdate.check_and_update(c, app_row(DIGEST_NEW))
        check("genuinely current -> no deploy", deployed == [], str(deployed))
        check("reported as updated False", r.get("updated") is False)
        check("and carries an affirmative verified True (CI must see a pass)",
              r.get("verified") is True, str(r.get("verified")))
    finally:
        patch(o)

    print("\n[the refresh hook's pass/fail rule]")
    def failed(result):
        return bool(result.get("error")) or result.get("verified") is not True

    check("rollback -> failed", failed({"updated": True, "verified": False}))
    check("error -> failed", failed({"error": "could not pull"}))
    check("verified no-op -> pass", not failed({"updated": False, "verified": True}))
    check("verified deploy -> pass", not failed({"updated": True, "verified": True}))
    check("a result with NO verified key -> failed (the old silent pass)",
          failed({"updated": False, "image": "x"}))

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
