"""Is the app actually serving? — the check both earlier outages needed.

Run it directly: `python tests/test_reachability.py` (no Docker, no database,
no network).

Replays both real incidents against the new checks:

  * 2026-09-28 — a container was deleted by a false-negative healthcheck
    rollback, ten times over ten hours. Scale-to-zero, so "no container" read as
    "asleep"; low traffic, so no 5xx; no alert. Found by a person.
  * 2026-11-02 — Traefik restarted on a reboot, failed to fetch its Sablier
    plugin before the network was up, and ran on without it. Every router using
    the plugin's middleware was rejected. Containers: up and healthy throughout.
    The fix written for the first outage passed cleanly. Found by a person.
"""
import asyncio
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for k in ("COOLIFY_TOKEN", "COOLIFY_PROJECT", "COOLIFY_SERVER",
          "COOLIFY_DESTINATION", "PAAS_DB_PASSWORD"):
    os.environ.setdefault(k, "x")
os.environ.setdefault("PAAS_DB_HOST", "localhost")

from app import probe  # noqa: E402

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: expected {want!r}, got {got!r}")
        FAILURES.append(label)


def main() -> None:
    print("router health (the 2026-11-02 outage)")

    # Verbatim shape of what Traefik reported that night.
    routers = [
        {"name": "http-0-xwe7yvl2x0jliurcwm8k3tv6@docker", "status": "disabled",
         "error": ['invalid middleware "sablier-smartbill-mcp@docker" configuration: '
                   'invalid middleware type or middleware does not exist']},
        {"name": "http-0-ujpoi2qao7qmypbd46d8jqh9@docker", "status": "disabled",
         "error": ['invalid middleware "sablier-revolut-mcp@docker" configuration: '
                   'invalid middleware type or middleware does not exist']},
        {"name": "fe-web@docker", "status": "enabled"},
    ]
    problems = probe.router_problems(routers)
    check("broken routers are reported", sorted(problems), [
        "http-0-ujpoi2qao7qmypbd46d8jqh9@docker",
        "http-0-xwe7yvl2x0jliurcwm8k3tv6@docker"])
    check("the reason is carried through",
          "invalid middleware" in problems["http-0-xwe7yvl2x0jliurcwm8k3tv6@docker"], True)
    check("healthy routers are not reported", "fe-web@docker" in problems, False)
    check("an all-healthy table is silent",
          probe.router_problems([{"name": "a@docker", "status": "enabled"}]), {})
    check("a disabled router with no error text still reports",
          probe.router_problems([{"name": "a@docker", "status": "disabled"}]),
          {"a@docker": "disabled"})

    print("\nreachability classification")
    # The edge saying it could not get there. pironman-web's "backend has no
    # route" is a 503, and a dead container behind Traefik is a 502.
    for status in (502, 503, 504):
        check(f"HTTP {status} is not serving", probe.is_serving(status), False)
    check("no response at all is not serving", probe.is_serving(None), False)
    # The app answering is the app answering; this measures routing, not auth.
    for status in (200, 301, 401, 403, 404, 418, 500):
        check(f"HTTP {status} counts as reached", probe.is_serving(status), True)

    print("\ndaily scheduling")
    hours = {a: probe.probe_hour(a) for a in
             ("smartbill-mcp", "revolut-mcp", "bt-gateway", "auditzel", "snake", "tasks-dev")}
    check("every hour is in range", all(0 <= h <= 23 for h in hours.values()), True)
    check("stable across calls", probe.probe_hour("smartbill-mcp"), hours["smartbill-mcp"])
    check("spread rather than all at once", len(set(hours.values())) > 1, True)

    aid = "smartbill-mcp"
    h = probe.probe_hour(aid)
    at_hour = datetime(2026, 11, 3, h, 30, tzinfo=timezone.utc)
    other = datetime(2026, 11, 3, (h + 1) % 24, 30, tzinfo=timezone.utc)
    check("due in its own hour", probe.due_for_probe(aid, at_hour, None), True)
    check("not due in another hour", probe.due_for_probe(aid, other, None), False)
    check("not due twice in one day",
          probe.due_for_probe(aid, at_hour, date(2026, 11, 3)), False)
    check("due again the next day",
          probe.due_for_probe(aid, at_hour, date(2026, 11, 2)), True)

    print("\nalerting (the 2026-09-28 outage: no traffic, so no 5xx to notice)")
    msgs, latch = probe.reachability_decision("smartbill-mcp", False, 503, "u", {})
    check("an unreachable app alerts", (len(msgs), latch), (1, True))
    check("the message names the symptom", "NOT serving" in msgs[0], True)

    msgs, latch = probe.reachability_decision(
        "smartbill-mcp", False, 503, "u", {"alerted_unreachable": True})
    check("latched: no repeat the next day", (msgs, latch), ([], True))

    msgs, latch = probe.reachability_decision(
        "smartbill-mcp", True, 200, "u", {"alerted_unreachable": True})
    check("recovery clears the latch", (len(msgs), latch), (1, False))

    msgs, latch = probe.reachability_decision("smartbill-mcp", True, 200, "u", {})
    check("a serving app is silent", (msgs, latch), ([], False))

    msgs, _ = probe.reachability_decision("smartbill-mcp", False, None, "u", {})
    check("a timeout reads as 'no response'", "no response" in msgs[0], True)

    print("\nretry (a daily check must not page on one dropped connection)")

    class Flaky:
        """Fails once, then succeeds."""
        def __init__(self, codes): self.codes = list(codes); self.n = 0
        async def get(self, url):
            code = self.codes[min(self.n, len(self.codes) - 1)]
            self.n += 1
            if code is None:
                raise OSError("connection reset")
            return type("R", (), {"status_code": code})()
        async def aclose(self): pass

    serving, status = asyncio.run(probe.probe("u", Flaky([None, 200])))
    check("one dropped connection then 200 -> serving", (serving, status), (True, 200))
    serving, status = asyncio.run(probe.probe("u", Flaky([503, 503])))
    check("consistently 503 -> not serving", (serving, status), (False, 503))
    serving, status = asyncio.run(probe.probe("u", Flaky([200])))
    check("a healthy app is probed once", serving, True)

    print("\nAPI unreadable is 'cannot tell', never 'healthy'")

    class Dead:
        async def get(self, url): raise OSError("refused")
        async def aclose(self): pass

    check("unreachable API returns None",
          asyncio.run(probe.fetch_routers("http://nope", Dead())), None)

    class NotJson:
        async def get(self, url):
            return type("R", (), {"status_code": 500, "json": lambda s=None: {}})()
        async def aclose(self): pass

    check("a non-200 API returns None",
          asyncio.run(probe.fetch_routers("http://nope", NotJson())), None)

    print()
    if FAILURES:
        print(f"FAILURES: {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
