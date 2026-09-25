"""A deploy Coolify accepted is not a deploy Coolify ran.

Run it directly: `python tests/test_label_landing.py`.

Coolify's deploy endpoint is asynchronous, and it will not start a second
`ApplicationDeploymentJob` for an app while one is already running. It does not
say so: the call returns `200 OK` and the request is dropped. Every layer above
then reports success for a change that never happened.

On 2026-09-25 `ping-pong` gained its first backend. `apply_image` deployed at
07:50:25.530; `apply_backend_labels`, which writes the marker condition that
keeps a fronted app's backend off its own public hostname, deployed again at
07:50:38.750. Both `200 OK`. Coolify ran ONE job (RUNNING 07:50:26, DONE
07:50:46) and the second request produced no queue row at all. The labels went
into Coolify's stored block and stayed there; the container kept the bare rule,
which is LONGER than the static host's and therefore wins Traefik's rule-length
tiebreak. Production served the backend's `{"status":"ok"}` at `/` instead of
the game, and nothing anywhere said so — Coolify returned 200, verify_deploy
returned verified (it watches the container, which really was up and healthy
while Coolify still had 11s of work left), and `sablier_enrolled` was written
true.

So the thing under test is not "were the labels written" — they always were. It
is "did they reach the container", which is the only question whose answer was
ever in doubt, and which nothing was asking.
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

from app import routing, sablier  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# The real thing: what ping-pong's backend must carry to stay off its own
# hostname, and what its container actually had.
MARKER = "Header(`X-Pironman-Backend`, `1`)"
R = "traefik.http.routers.http-0-utbu9xwlslhsbyvm1rlgvgyi"
DESIRED = {
    f"{R}.rule": f"Host(`ping-pong-coolify.bogdanripa.com`) && PathPrefix(`/`) "
                 f"&& {MARKER}",
    f"{R}.priority": "100",
    f"{R}.middlewares": "sablier-ping-pong,gzip",
    "sablier.enable": "true",
    "sablier.group": "ping-pong",
}
UNSCOPED = {
    f"{R}.rule": "Host(`ping-pong-coolify.bogdanripa.com`) && PathPrefix(`/`)",
    f"{R}.middlewares": "gzip",
}
# Coolify writes a great deal besides, and it churns every deploy.
COOLIFY_NOISE = {
    "coolify.managed": "true",
    "com.docker.compose.config-hash": "e6540ed9a8a3",
    "caddy_0": "http://ping-pong-coolify.bogdanripa.com",
    "traefik.enable": "true",
    "traefik.http.middlewares.gzip.compress": "true",
}


class FakeBox:
    """Coolify and the container, with Coolify's dropping behaviour modelled.

    The caller of `_confirm_labels_landed` has ALREADY deployed once, so the
    starting state is the outcome of that deploy: `needed=0` means it took and
    the container already carries the labels; `needed=1` is the ping-pong
    incident, where Coolify answered 200 and dropped it, so one further deploy
    is required. Counting from the caller's deploy rather than from this
    function is the whole point — the first request is the one that went
    missing.
    """

    def __init__(self, needed: int):
        self.needed = needed
        self.deploys = []
        self.labels = {**COOLIFY_NOISE,
                       **(UNSCOPED if needed else DESIRED)}

    async def deploy(self, uuid, *, app_id, reason):
        self.deploys.append(reason)
        self.needed -= 1
        if self.needed <= 0:            # this one was not dropped
            self.labels = {**COOLIFY_NOISE, **DESIRED}

    async def current_labels(self, uuid):
        return dict(self.labels)


def install(box):
    routing.coolify.deploy = box.deploy
    sablier._current_labels = box.current_labels
    routing.sablier._current_labels = box.current_labels


def fast():
    """No real waiting — the windows are counted in sleeps, so a no-op sleep
    makes a 90s window 18 iterations of nothing."""
    async def _noop(_):
        return None
    routing.asyncio.sleep = _noop


def main():
    fast()

    print("[the labels land on the first deploy: nothing extra happens]")
    box = FakeBox(needed=0)
    install(box)
    ok = asyncio.run(routing._confirm_labels_landed("ping-pong", "u", DESIRED))
    # The caller already deployed; confirming must not deploy again when there
    # is nothing wrong. A confirmation that always redeploys is a redeploy loop.
    check("it confirms", ok)
    check("and asked for NO further deploy", box.deploys == [], str(box.deploys))

    print("\n[the ping-pong incident: one accepted-and-dropped deploy]")
    box = FakeBox(needed=1)
    install(box)
    ok = asyncio.run(routing._confirm_labels_landed("ping-pong", "u", DESIRED))
    check("it still gets there", ok)
    check("because it asked again, exactly once", len(box.deploys) == 1,
          str(box.deploys))
    check("and said why", box.deploys and "never reached the container"
          in box.deploys[0], str(box.deploys[:1]))
    check("the container now carries the marker",
          MARKER in box.labels[f"{R}.rule"])
    check("and the sablier enrollment that was lost with it",
          box.labels.get("sablier.enable") == "true")

    print("\n[it gives up rather than deploying for ever]")
    box = FakeBox(needed=99)
    install(box)
    ok = asyncio.run(routing._confirm_labels_landed("ping-pong", "u", DESIRED))
    check("it reports failure instead of claiming success", ok is False)
    check("bounded retries", len(box.deploys) == routing.LABEL_ATTEMPTS - 1,
          f"{len(box.deploys)} extra deploys")

    print("\n[Coolify's own labels must not be mistaken for a mismatch]")
    # The check is a subset test for exactly this reason: Coolify rewrites its
    # bookkeeping on every deploy, and an equality check would never match, so
    # the app would be redeployed on every sync for ever.
    box = FakeBox(needed=0)
    install(box)
    box.labels = {**COOLIFY_NOISE, **DESIRED,
                  "com.docker.compose.config-hash": "something-completely-new",
                  "coolify.version": "4.3.23",
                  "traefik.http.routers.other.rule": "Host(`unrelated`)"}
    ok = asyncio.run(routing._confirm_labels_landed("ping-pong", "u", DESIRED))
    check("extra labels are fine", ok)
    check("and nothing was redeployed over them", box.deploys == [],
          str(box.deploys))

    print("\n[a single wrong value is still a mismatch]")
    for key, bad in ((f"{R}.rule", UNSCOPED[f"{R}.rule"]),   # the marker gone
                     (f"{R}.priority", "1"),
                     ("sablier.enable", "false")):
        box = FakeBox(needed=99)
        install(box)
        box.labels = {**COOLIFY_NOISE, **DESIRED, key: bad}
        ok = asyncio.run(routing._confirm_labels_landed("x", "u", DESIRED))
        check(f"wrong {key.split('.')[-1]} is caught", ok is False)

    print("\n[what counts as operative]")
    ops = routing._operative({**COOLIFY_NOISE, **DESIRED})
    check("the router rule is operative", f"{R}.rule" in ops)
    check("priority is operative", f"{R}.priority" in ops)
    check("sablier.* is operative", "sablier.enable" in ops)
    check("the sablier middleware is operative",
          "sablier-x" in routing._operative(
              {"traefik.http.middlewares.sablier-x.plugin.sablier.group": "x"}
          ).popitem()[0])
    for noise in ("coolify.managed", "com.docker.compose.config-hash", "caddy_0",
                  "traefik.enable", "traefik.http.middlewares.gzip.compress"):
        check(f"{noise} is not", noise not in ops)

    print("\n[an app with nothing operative is not held up]")
    # unscoped() can legitimately leave a container with no routers at all (an
    # internal app). Waiting 4.5 minutes for labels that were never written
    # would turn a no-op into a stall.
    box = FakeBox(needed=99)
    install(box)
    check("it returns at once",
          asyncio.run(routing._confirm_labels_landed("x", "u", COOLIFY_NOISE)))
    check("without deploying", box.deploys == [], str(box.deploys))

    print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
    return 1 if fails else 0


sys.exit(main())
