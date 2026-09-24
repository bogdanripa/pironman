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


async def start(deploy_id: str, app_id: str, kind: str = "backend",
                commit: str | None = None, **detail) -> None:
    """Record that a deploy has been queued. Written before the 202 is sent, so
    the id the caller is handed is already resolvable when it first polls."""
    async with pool().acquire() as c:
        await c.execute(
            "INSERT INTO deploys (id, app_id, kind, commit_sha, detail) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (id) DO NOTHING",
            deploy_id, app_id, kind, _short(commit), detail or None)


def _short(commit: str | None) -> str | None:
    """A full 40-character sha is what CI has and a 7-character one is what a
    person reads. Stored short, because everything that consumes this — a
    report, a comparison against `git log --oneline`, a question of "is this the
    build I think it is" — is done by eye."""
    if not commit:
        return None
    commit = commit.strip()
    return commit[:7] if len(commit) >= 7 else commit


async def record(app_id: str, kind: str, ok: bool, commit: str | None = None,
                 **detail) -> None:
    """Write a deploy that was never queued: one that began and ended inside a
    single request. A frontend upload is exactly that — no container, no image,
    no polling — so it has no deploy id to hand back, but it is still a deploy
    and "what is live" is wrong without it.

    Best-effort like finish(): an upload that happened but could not be written
    down is better than one that fails because the bookkeeping did.
    """
    import uuid
    try:
        async with pool().acquire() as c:
            await c.execute(
                "INSERT INTO deploys (id, app_id, kind, commit_sha, ok, "
                "finished_at, detail) VALUES ($1, $2, $3, $4, $5, now(), $6)",
                f"{kind}-{uuid.uuid4().hex[:12]}", app_id, kind,
                _short(commit), ok, detail or None)
    except Exception:
        _log.warning("could not record the %s deploy of %s", kind, app_id,
                     exc_info=True)


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


async def status(app_id: str | None = None, limit: int = 20) -> list[dict]:
    """What is live, per app, for each half of a deploy.

    Two rows per app at most — the latest `backend` and the latest `frontend` —
    because an app that ships both runs them as separate CI jobs that finish at
    different times. Collapsing them into one "last deploy" is how a stale
    bundle hides behind a fresh image: the timestamp moves, so everything looks
    shipped, and the half that failed is the half nobody looked at.

    Without this, the only way to answer "is what I just pushed live?" is to
    compare a deploy time against a commit time and hope. That is a guess
    dressed as a fact, and it is wrong in exactly the cases that matter: a
    rolled-back deploy leaves a container that is newer than the commit and
    running the previous build.
    """
    sql = """
        SELECT DISTINCT ON (app_id, kind)
               app_id, kind, id, queued_at, finished_at, ok, commit_sha, detail,
               queued_at < now() - interval '{stale}' AS stale
        FROM deploys
        {where}
        ORDER BY app_id, kind, queued_at DESC
    """.format(stale=STALE_AFTER, where="WHERE app_id = $1" if app_id else "")
    async with pool().acquire() as c:
        rows = await c.fetch(sql, *( [app_id] if app_id else [] ))

    by_app: dict[str, dict] = {}
    for r in rows:
        if r["ok"] is None:
            state = "unknown" if r["stale"] else "running"
        else:
            state = "succeeded" if r["ok"] else "failed"
        entry = {
            "state": state,
            "at": (r["finished_at"] or r["queued_at"]).isoformat(),
            "commit": r["commit_sha"],
            "deploy": r["id"],
        }
        if r["commit_sha"] is None:
            # Said out loud rather than left as a null to interpret. An older
            # deploy predates the workflows that send it; a recent one means the
            # repo is still on a workflow that does not.
            entry["commit_note"] = (
                "this pipeline did not report a commit — regenerate the "
                "workflow with apps_deploy_workflow, which sends it")
        if r["detail"]:
            entry["detail"] = r["detail"]
        by_app.setdefault(r["app_id"], {"app": r["app_id"]})[r["kind"]] = entry

    out = sorted(by_app.values(), key=lambda a: a["app"])
    for app in out:
        halves = [k for k in ("backend", "frontend") if k in app]
        app["summary"] = ", ".join(
            f"{k} {app[k]['state']}"
            + (f" ({app[k]['commit']})" if app[k].get("commit") else "")
            for k in halves) or "no deploy on record"
    return out[:limit]
