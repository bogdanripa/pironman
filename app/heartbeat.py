"""Liveness for the control plane's background loops.

Every failure this platform has spent real time on shares one shape: something
reported success while doing nothing. A rolled-back deploy left the old
container answering. An app marked to sleep never slept. An analytics cursor
froze for a day while each pass "succeeded" having counted zero.

Background loops are the worst case of that, because a loop has no observer. It
cannot notice its own staleness — counting nothing is exactly what a quiet box
looks like — and it produces no signal when it is idle, so a dead loop and a
bored one are byte-identical in the logs.

So each loop states two things: when it last COMPLETED, and how long it may
reasonably go without completing. That turns "is this working?" from an
inference into a comparison anyone can make, including a reader outside this
process — which is the point. Nothing running inside paas-api can report that
paas-api is wedged.

Recording is best-effort by design: a heartbeat that cannot be written must
never be the reason a loop dies. A missing beat reads as stale, which is the
safe direction to fail in.
"""
import logging

from .db import pool

_log = logging.getLogger("pironman.heartbeat")

# task -> how long without a successful completion counts as stalled.
#
# Generous multiples of each loop's real cadence, not tight bounds: one slow
# pass, a redeploy or a Docker hiccup must not page anyone. These are chosen to
# catch a loop that has STOPPED, which is the failure that actually happens —
# the analytics cursor sat frozen for 23 hours.
STALE_AFTER = {
    "analytics_ingest": 600,    # runs every 120s
    "alerts_check":     900,    # runs every 150s
    "autoupdate_sweep": 5400,   # hourly, plus room for a long sweep
    "cron_dispatch":    300,    # host-side dispatcher, every minute
}


async def beat(task: str, error: str | None = None) -> None:
    """Record that `task` just finished. Pass `error` to record a failed run.

    A failure updates last_error but deliberately leaves last_ok alone: the
    question a watchdog asks is "when did this last actually work?", and a loop
    that is failing every pass must keep looking stale rather than reset its own
    clock by reporting the attempt.
    """
    stale = STALE_AFTER.get(task, 900)
    try:
        async with pool().acquire() as c:
            if error is None:
                await c.execute(
                    "INSERT INTO task_heartbeat "
                    "  (task, last_ok, last_error, last_error_at, stale_after_s) "
                    "VALUES ($1, now(), NULL, NULL, $2) "
                    "ON CONFLICT (task) DO UPDATE SET last_ok = now(), "
                    "  last_error = NULL, last_error_at = NULL, "
                    "  stale_after_s = EXCLUDED.stale_after_s",
                    task, stale)
            else:
                await c.execute(
                    "INSERT INTO task_heartbeat "
                    "  (task, last_ok, last_error, last_error_at, stale_after_s) "
                    "VALUES ($1, NULL, $2, now(), $3) "
                    "ON CONFLICT (task) DO UPDATE SET last_error = EXCLUDED.last_error, "
                    "  last_error_at = now(), stale_after_s = EXCLUDED.stale_after_s",
                    task, error[:500], stale)
    except Exception:
        # Never let bookkeeping kill the work it is bookkeeping for.
        _log.exception("could not record heartbeat for %s", task)


async def snapshot() -> list[dict]:
    """Every task's liveness, newest-stale first.

    `stalled` is the answer to the only question worth asking, computed against
    each task's own declared tolerance so a caller needs no schedule table. A
    task that has never completed reads stalled rather than unknown — on a box
    that has been up for a while, never having run is the same problem.
    """
    async with pool().acquire() as c:
        rows = await c.fetch(
            "SELECT task, last_ok, last_error, last_error_at, stale_after_s, "
            "  EXTRACT(EPOCH FROM (now() - last_ok))::int AS age_s "
            "FROM task_heartbeat ORDER BY task")
    out = []
    for r in rows:
        age = r["age_s"]
        stalled = age is None or age > r["stale_after_s"]
        out.append({
            "task": r["task"],
            "last_ok": r["last_ok"].isoformat() if r["last_ok"] else None,
            "seconds_since_ok": age,
            "stale_after_s": r["stale_after_s"],
            "stalled": stalled,
            "last_error": r["last_error"],
            "last_error_at": (r["last_error_at"].isoformat()
                              if r["last_error_at"] else None),
        })
    out.sort(key=lambda d: (not d["stalled"], d["task"]))
    return out
