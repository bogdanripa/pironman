"""Read-only analytics tools over the cross-app Traefik access log.

All three tools work the same way: pass an app_id to scope to one app, or omit
it for the whole platform. The rollups are built automatically in the background
(see app/analytics.py) — there is nothing to instrument per app.
"""
from fastapi import APIRouter, Depends, Query

from ..auth import require_key
from .. import analytics

router = APIRouter(prefix="/analytics", tags=["analytics"],
                   dependencies=[Depends(require_key)])


@router.get("/overview", operation_id="analytics_overview",
            summary="Unique visitors, hits and DAU/WAU/MAU, overall or per app")
async def overview(
    app_id: str | None = Query(None, description="Scope to one app; omit for all apps"),
    days: int = Query(30, ge=1, le=365, description="Window in days for the totals"),
):
    """Headline traffic numbers: unique visitors and hits over the window, plus
    daily / weekly / monthly active visitors (distinct visitors in the last 1 / 7
    / 30 days). Called with no app_id it also breaks the totals down per app, so
    it doubles as the platform-wide dashboard. A visitor is a cookieless
    salted hash of IP + user-agent, counted once across apps."""
    return await analytics.overview(app_id, days)


@router.get("/timeseries", operation_id="analytics_timeseries",
            summary="Daily unique visitors and hits for a line chart")
async def timeseries(
    app_id: str | None = Query(None, description="Scope to one app; omit for all apps"),
    days: int = Query(30, ge=1, le=365, description="Number of days back to return"),
):
    """Per-day unique visitors and hits over the window — the series to plot as a
    traffic line chart. Days with no traffic are omitted rather than zero-filled."""
    return await analytics.timeseries(app_id, days)


@router.get("/cohorts", operation_id="analytics_cohorts",
            summary="Weekly retention cohorts (returning visitors over time)")
async def cohorts(
    app_id: str | None = Query(None, description="Scope to one app; omit for all apps"),
    weeks: int = Query(8, ge=2, le=52, description="How many weekly cohorts back"),
):
    """Weekly retention: visitors are grouped by the week they were first seen,
    and each cohort shows how many came back in each following week. `retained[0]`
    is the cohort's own size; `retained[N]` is how many returned N weeks later,
    with `retention_pct` the same as a percentage of the cohort. Globally, "first
    seen" means first seen on any app."""
    return await analytics.cohorts(app_id, weeks)
