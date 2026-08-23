"""The outcome of a CI deploy, kept where the caller can come back for it.

`POST /apps/<id>/refresh` used to hold the connection open until the deploy was
verified. That is 20-30s for an app whose image the box already has, but a
first-ever deploy pulls the image onto a Pi, and two of those measured 125s at
the edge — past Cloudflare's ~100s ceiling, which cut the connection and gave CI
a 524 for a deploy that was running perfectly well and went on to succeed. A
slow-but-fine deploy was indistinguishable from a broken one.

So the hook answers 202 the moment the deploy is queued and finishes off-request.
The verification it used to return is not dropped — it is written here, and
`GET /apps/<id>/refresh` reads it back. That matters more than the convenience:
Coolify rolls a failed deploy back silently and the previous container keeps
serving, so a caller with no way to ask the outcome would see a healthy app and
call the deploy a success. That is the one failure this platform is built
against (§12), and it is why "return early" had to come with somewhere to look.

Postgres and not a dict in memory, for the same reason platform_events is a
table: the control plane is an app on this box and redeploys often, so
in-process state disappears at precisely the moment someone is mid-poll — and a
lost result reads as "still running", which is the wrong direction to guess in.
"""
import logging

from .db import pool

_log = logging.getLogger("pironman.deploys")

# A deploy still marked running after this long is not running: nothing in the
# path waits that long, so the control plane was recycled mid-deploy and the task
# died with it. Reported as "unknown" rather than left as "running" for ever —
# saying "I lost track of this" is honest, and a caller can act on it; a
# permanent "running" is a poll loop that never ends.
STALE_AFTER = "20 minutes"

RETAIN_DAYS = 30


async def start(deploy_id: str, app_id: str, **detail) -> None:
    """Record that a deploy has been queued. Written before the 202 is sent, so
    the id the caller is handed is already resolvable when it first polls."""
    async with pool().acquire() as c:
        await c.execute(
            "INSERT INTO deploys (id, app_id, detail) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING",
            deploy_id, app_id, detail or None)


async def finish(deploy_id: str, ok: bool, **detail) -> None:
    """Record how a deploy ended. Best-effort — a deploy that happened but could
    not be written down is better than one that fails because the bookkeeping
    did, and an unwritten row reads as "unknown", which is the safe direction."""
    try:
        async with pool().acquire() as c:
            await c.execute(
                "UPDATE deploys SET ok = $2, finished_at = now(), "
                "detail = COALESCE(detail, '{}'::jsonb) || $3::jsonb "
                "WHERE id = $1",
                deploy_id, ok, detail or {})
    except Exception:
        _log.warning("could not record the outcome of deploy %s", deploy_id,
                     exc_info=True)


async def get(app_id: str, deploy_id: str | None = None) -> dict | None:
    """One deploy's state: the named one, or the app's most recent.

    `state` is what a caller should branch on — "succeeded", "failed", "running"
    or "unknown". The last covers both a deploy nobody has a record of and one
    whose control plane was recycled while it ran; neither can be called a pass
    or a fail, and neither should be reported as one.
    """
    async with pool().acquire() as c:
        if deploy_id:
            row = await c.fetchrow(
                "SELECT id, app_id, queued_at, finished_at, ok, detail, "
                f"queued_at < now() - interval '{STALE_AFTER}' AS stale "
                "FROM deploys WHERE id = $1 AND app_id = $2", deploy_id, app_id)
        else:
            row = await c.fetchrow(
                "SELECT id, app_id, queued_at, finished_at, ok, detail, "
                f"queued_at < now() - interval '{STALE_AFTER}' AS stale "
                "FROM deploys WHERE app_id = $1 "
                "ORDER BY queued_at DESC, id DESC LIMIT 1", app_id)
    if not row:
        return None
    if row["ok"] is None:
        state = "unknown" if row["stale"] else "running"
    else:
        state = "succeeded" if row["ok"] else "failed"
    return {
        "deploy": row["id"],
        "app": row["app_id"],
        "state": state,
        "queued_at": row["queued_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
        "detail": row["detail"] or {},
        **({"note": "the control plane was recycled while this deploy was "
                    "running, so its outcome was never written down — check the "
                    "app itself rather than treating this as a pass or a fail"}
           if state == "unknown" and row["stale"] else {}),
    }


async def trim() -> None:
    """Drop deploy records past the retention window. Best-effort, at startup —
    same contract as events.trim: a bookkeeping table must never keep the control
    plane from booting."""
    try:
        async with pool().acquire() as c:
            await c.execute(
                f"DELETE FROM deploys WHERE queued_at < now() - interval "
                f"'{RETAIN_DAYS} days'")
    except Exception:
        _log.warning("could not trim deploys", exc_info=True)
