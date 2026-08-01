"""Live infrastructure view: per-app running state, CPU/RAM, database size and
recent request health, plus host totals. Read-only. On its own /stats prefix so
the static path never collides with /apps/{app_id}."""
from fastapi import APIRouter, Depends, Query

from ..auth import require_key
from .. import heartbeat, stats

router = APIRouter(prefix="/stats", tags=["stats"],
                   dependencies=[Depends(require_key)])


@router.get("/apps", operation_id="apps_stats",
            summary="Per-app running state, live CPU/RAM, DB size and request health")
async def apps_stats(
    include_db: bool = Query(True, description="Probe each database for its size "
                             "(a little slower); set false for CPU/RAM only"),
    traffic_days: int = Query(7, ge=1, le=90,
                              description="Window for the request/error/latency stats"),
):
    """A live snapshot of every app's resource use, in one call:

    - **running** — true only if the app's container is currently up (a crashed
      or rolled-back deploy reads as false).
    - **cpu_pct / mem_mb / mem_pct** — sampled live from `docker stats` (cpu_pct
      is share of one core, so >100% means multiple cores).
    - **db_size_mb** — on-disk size of the app's database, or null if it has none.
    - **last_seen** — when the app was last requested (any traffic, all time), or
      null if it has never been seen in the access log.
    - **traffic** — requests, error_pct (4xx+5xx), server_error_pct (5xx) and
      avg_ms over the last `traffic_days`, from the access-log rollup.

    Also returns **host** totals (CPU count, total RAM, RAM currently used by
    containers) so you can see the Pi's remaining headroom at a glance."""
    return await stats.app_resources(include_db=include_db, perf_days=traffic_days)


@router.get("/tasks", operation_id="platform_tasks_health",
            summary="Liveness of the platform's own background loops")
async def tasks_health():
    """Whether the control plane's background work is actually happening.

    Every loop here fails the same way when it fails: it keeps running and stops
    achieving anything. Nothing about the container says so — it stays up and
    healthy — and the loops are silent when idle, so a dead one and a bored one
    look identical in the logs. Each therefore records when it last *completed*,
    and how long it may go without completing before that counts as stalled.

    Read this when a platform-wide symptom has no obvious owner: analytics that
    stopped moving, an app that never sleeps, a deploy that never swept, a
    scheduled job that silently stopped firing. A `stalled` task names the
    culprit directly instead of leaving it to be inferred from side effects.

    - **analytics_ingest** — folds the proxy access log into the rollups. Stalled
      means every traffic number and last-accessed time is frozen.
    - **alerts_check** — down/recovered/5xx notifications. Stalled means outages
      go unannounced.
    - **autoupdate_sweep** — hourly image check, route sync and Sablier repair.
      Stalled means drift stops being repaired; note this loop waits an hour
      before its first run, so a control plane that redeploys more often than
      that can go indefinitely without ever sweeping.
    - **cron_dispatch** — the host-side scheduled-job dispatcher. It writes its
      own beat, so a missing or stale one means jobs are not firing at all.

    A task absent from the list has never reported since the table was created.
    """
    rows = await heartbeat.snapshot()
    stalled = [r["task"] for r in rows if r["stalled"]]
    return {
        "tasks": rows,
        "stalled": stalled,
        "healthy": not stalled,
        "note": ("all background work is current" if not stalled else
                 "stalled: " + ", ".join(stalled) + " — these are not doing "
                 "their work, whatever the container status says"),
    }
