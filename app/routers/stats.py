"""Live infrastructure view: per-app running state, CPU/RAM, database size and
recent request health, plus host totals. Read-only. On its own /stats prefix so
the static path never collides with /apps/{app_id}."""
from fastapi import APIRouter, Depends, Query

from ..auth import require_key
from .. import stats

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
