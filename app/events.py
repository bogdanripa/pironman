"""A durable record of every time this platform recycles a container.

Why a table and not a log line. The control plane's own stdout is the first
thing lost when the control plane is recycled: a Coolify deploy builds a NEW
container, and `docker logs` on it starts empty. So the one event you most need
afterwards — what restarted this, and why — is the one the container cannot
still be holding. Postgres outlives the container; stdout does not.

That is not hypothetical. `api` was quietly enrolled in scale-to-zero by a repair
pass, after which it slept five minutes after every request and could not wake
itself. Reconstructing that took reading a container log that had already been
rotated out by later restarts, a proxy access log, and the container's labels.
One row saying "api / deploy / enrolled in sablier (sleep_when_idle=true)" would
have answered it outright.

Recording is best-effort, exactly like heartbeat: a write that fails must never
be the reason a deploy fails. A missing row reads as "unknown", which is the
safe direction — the alternative is a platform that cannot recycle anything
because its audit table is unhappy.

What this DOES NOT cover, and cannot: containers stopped by Sablier. Sablier is
its own process acting on container labels, so nothing here observes it. What
this catches instead is the decision upstream of it — the write that put those
labels on the container in the first place, which is ours and is recorded.
"""
import json
import logging

from .db import pool

_log = logging.getLogger("pironman.events")

# Deploys are rare (a handful a day on a busy day), so 90 days is a few hundred
# rows. Trimmed at startup rather than on a timer: the cost of an oversized table
# here is nil, and a trim in the write path would put a DELETE in front of every
# deploy for no benefit.
RETAIN_DAYS = 90


async def record(app_id: str, action: str, reason: str, **detail) -> None:
    """Note that the platform recycled `app_id`, and why.

    `reason` is prose for a human reading this six weeks later — say what decided
    and what it observed ("image digest moved", "enrolled in sablier"), not which
    function ran. Anything structured goes in **detail.
    """
    try:
        async with pool().acquire() as c:
            await c.execute(
                "INSERT INTO platform_events (app_id, action, reason, detail) "
                "VALUES ($1, $2, $3, $4)",
                app_id, action, reason,
                json.dumps(detail) if detail else None)
    except Exception:
        # Deliberately not re-raised: see the module docstring. Logged at warning
        # so a persistently unwritable table is still visible while it lasts.
        _log.warning("could not record %s/%s: %s", app_id, action, reason,
                     exc_info=True)


async def trim() -> None:
    """Drop events past the retention window. Best-effort, called at startup."""
    try:
        async with pool().acquire() as c:
            await c.execute(
                f"DELETE FROM platform_events WHERE at < now() - interval "
                f"'{RETAIN_DAYS} days'")
    except Exception:
        _log.warning("could not trim platform_events", exc_info=True)


async def recent(limit: int = 100, app_id: str | None = None) -> list[dict]:
    """The most recent recycle events, newest first."""
    q = ("SELECT id, at, app_id, action, reason, detail FROM platform_events "
         "{where} ORDER BY at DESC, id DESC LIMIT $1")
    async with pool().acquire() as c:
        if app_id:
            rows = await c.fetch(q.format(where="WHERE app_id = $2"),
                                 limit, app_id)
        else:
            rows = await c.fetch(q.format(where=""), limit)
    return [{"id": r["id"], "at": r["at"].isoformat(), "app_id": r["app_id"],
             "action": r["action"], "reason": r["reason"],
             "detail": json.loads(r["detail"]) if r["detail"] else None}
            for r in rows]
