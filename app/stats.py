"""Live per-app resource usage: is it running, CPU/RAM right now, database size,
and recent request health — all from plumbing that already exists.

CPU/memory come from a single `docker stats --no-stream` over the mounted socket
(one sample for every container at once). Database size reuses the `docker exec`
path (provision.db_size). Request volume / error rate / average latency come from
the analytics_perf rollup the access-log ingester already fills, so they cost a
query, not a probe. Host totals come from `docker info`.
"""
import asyncio
import re

from . import autoupdate, provision
from .db import pool

_MEM_UNITS = {"B": 1 / 1048576, "KIB": 1 / 1024, "MIB": 1.0,
              "GIB": 1024.0, "TIB": 1048576.0}


def _mem_mb(tok: str) -> float | None:
    """'12.3MiB' -> 12.3 (megabytes). Docker uses binary (…iB) units."""
    m = re.match(r"([0-9.]+)\s*([A-Za-z]+)", tok.strip())
    if not m:
        return None
    factor = _MEM_UNITS.get(m.group(2).upper())
    return round(float(m.group(1)) * factor, 1) if factor else None


def _pct(tok: str) -> float | None:
    try:
        return round(float(tok.strip().rstrip("%")), 2)
    except ValueError:
        return None


async def _container_stats() -> dict[str, dict]:
    """name -> {cpu_pct, mem_mb, mem_pct} for every *running* container. Stopped
    containers do not appear — that absence is how we tell an app is down."""
    _, out = await autoupdate._docker(
        "stats", "--no-stream", "--format",
        "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}", timeout=40)
    res: dict[str, dict] = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) < 4:
            continue
        res[p[0]] = {"cpu_pct": _pct(p[1]),
                     "mem_mb": _mem_mb(p[2].split("/")[0]),
                     "mem_pct": _pct(p[3])}
    return res


async def _host(stats: dict[str, dict]) -> dict:
    """Host capacity (CPUs, total RAM) plus what containers are using right now."""
    _, out = await autoupdate._docker(
        "info", "--format", "{{.NCPU}} {{.MemTotal}}", timeout=20)
    ncpu = mem_total = None
    parts = out.split()
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        ncpu = int(parts[0])
        mem_total = round(int(parts[1]) / 1048576, 1)
    used = round(sum(v["mem_mb"] for v in stats.values() if v.get("mem_mb")), 1)
    return {"ncpu": ncpu, "mem_total_mb": mem_total,
            "mem_used_by_containers_mb": used}


async def _perf_map(conn, days: int) -> dict[str, dict]:
    rows = await conn.fetch(
        "SELECT app_id, SUM(requests) req, SUM(err_client) ec, "
        "       SUM(err_server) es, SUM(dur_ms_sum) dur "
        "FROM analytics_perf WHERE day > CURRENT_DATE - $1::int GROUP BY app_id",
        days)
    out: dict[str, dict] = {}
    for r in rows:
        req = r["req"] or 0
        out[r["app_id"]] = {
            "requests": req,
            "error_pct": round(100 * ((r["ec"] or 0) + (r["es"] or 0)) / req, 2)
                         if req else 0,
            "server_error_pct": round(100 * (r["es"] or 0) / req, 2) if req else 0,
            "avg_ms": round((r["dur"] or 0) / req, 1) if req else None,
        }
    return out


async def app_resources(include_db: bool = True, perf_days: int = 7) -> dict:
    """Per-app resource snapshot plus host totals. `include_db=False` skips the
    (slightly slower) database-size probe for a faster CPU/RAM-only view."""
    async with pool().acquire() as conn:
        apps = await conn.fetch(
            "SELECT id, coolify_uuid, db_engine, db_user, db_password, db_name "
            "FROM apps ORDER BY id")
        perf = await _perf_map(conn, perf_days)

    stats = await _container_stats()
    names = list(stats.keys())

    async def db_mb(app) -> float | None:
        if not include_db or not app["db_engine"]:
            return None
        try:
            b = await provision.db_size(app["db_engine"], app["db_name"],
                                        app["db_user"], app["db_password"])
        except Exception:
            return None
        return round(b / 1048576, 1) if b is not None else None

    async def one(app) -> dict:
        uuid = app["coolify_uuid"] or ""
        cname = next((n for n in names if uuid and uuid in n), None)
        s = stats.get(cname, {})
        return {
            "id": app["id"],
            "running": cname is not None,
            "cpu_pct": s.get("cpu_pct"),
            "mem_mb": s.get("mem_mb"),
            "mem_pct": s.get("mem_pct"),
            "db_engine": app["db_engine"],
            "db_size_mb": await db_mb(app),
            "traffic": perf.get(app["id"], {"requests": 0, "error_pct": 0,
                                            "server_error_pct": 0, "avg_ms": None}),
        }

    rows = await asyncio.gather(*[one(a) for a in apps])
    return {"apps": list(rows), "host": await _host(stats),
            "traffic_window_days": perf_days}
