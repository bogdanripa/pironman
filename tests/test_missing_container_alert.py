"""A scale-to-zero app whose container was DELETED must alert, even with no traffic.

Run it directly: `python tests/test_missing_container_alert.py` (nothing here
touches Docker, Coolify or the database).

This is the shape of a real ten-hour outage. smartbill-mcp's container was
removed by a rollback, the hourly self-repair redeployed it ten times and the
same rollback removed it again each time, and nothing said a word. Two design
decisions combined:

  * a scale-to-zero app is deliberately exempt from the "is down" alert, because
    a stopped container is the feature working; and
  * the exemption's stated cover was the app's 5xx rate — which only exists if
    somebody is making requests. A low-traffic app with no container produces no
    requests, therefore no 5xx, therefore no alert.

The outage was found by a human trying to use the app. "Stopped" and "deleted"
are indistinguishable to every other check in this file and could not be further
apart: Sablier wakes an instance through a label on the container, so a deleted
one has no group and can never wake, whatever the traffic.
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

from app import alerts  # noqa: E402

UUID = "xwe7yvl2x0jliurcwm8k3tv6"
APP = "smartbill-mcp"


def state(missing_count=0, alerted_missing=False, **kw):
    base = {"fail_count": 0, "alerted_down": False, "alerted_stuck": False,
            "missing_count": missing_count, "alerted_missing": alerted_missing,
            "err_day": None, "err_server": 0}
    base.update(kw)
    return base


def decide(exists: bool, prev: dict | None, sleeps: bool = True):
    """The real decision, not a copy of it — drift here fails the test."""
    return alerts.missing_container_decision(APP, exists, prev, sleeps)


def check(label, got, want):
    assert got == want, f"{label}: expected {want!r}, got {got!r}"
    print(f"  ok   {label}")


def main() -> None:
    print("missing-container alerting")

    # One missed check is a deploy swapping the container, not an outage.
    msgs, missing, alerted = decide(exists=False, prev=state())
    check("first check with no container -> silent (debounce)", (msgs, missing, alerted), ([], 1, False))

    # Two in a row is the real thing.
    msgs, missing, alerted = decide(exists=False, prev=state(missing_count=1))
    check("second consecutive -> alerts", (len(msgs), missing, alerted), (1, 2, True))

    # Latches: it does not page every 150s for the same outage.
    msgs, missing, alerted = decide(exists=False, prev=state(missing_count=2, alerted_missing=True))
    check("still missing -> no repeat alert", (msgs, alerted), ([], True))

    # The hourly repair briefly creates a container before the rollback removes
    # it. That must clear the latch, so the NEXT disappearance alerts again
    # rather than being swallowed by a stale flag.
    msgs, missing, alerted = decide(exists=True, prev=state(missing_count=5, alerted_missing=True))
    check("container returns -> recovery, counter reset", (len(msgs), missing, alerted), (1, 0, False))

    # A normal sleeping app: stopped but present. This is the case the exemption
    # exists for and it must stay silent.
    msgs, missing, alerted = decide(exists=True, prev=state())
    check("asleep with a container -> silent", (msgs, missing, alerted), ([], 0, False))

    # First ever sighting (no prior row) never alerts: a freshly created app has
    # no container yet and must not page on birth.
    msgs, missing, alerted = decide(exists=False, prev=None)
    check("no previous state -> silent", (msgs, alerted), ([], False))

    # A non-sleeping app is covered by the "is down" alert; saying it twice
    # trains people to skim past both.
    msgs, _, _ = decide(exists=False, prev=state(missing_count=9), sleeps=False)
    check("non-sleeping app -> not duplicated here", msgs, [])

    # A stale latch from before the app became non-sleeping must not mute a
    # later genuine recovery.
    _, _, alerted = decide(exists=False, prev=state(alerted_missing=True), sleeps=False)
    check("stale latch cleared when app stops sleeping", alerted, False)

    print("\nALL PASS")


if __name__ == "__main__":
    main()
