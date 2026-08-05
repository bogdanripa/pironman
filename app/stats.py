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
import shutil
from datetime import datetime, timezone

from . import autoupdate, provision
from .analytics import LATENCY_BUCKETS_MS
from .config import STATIC_HOST_APP
from .db import pool

_MEM_UNITS = {"B": 1 / 1048576, "KIB": 1 / 1024, "MIB": 1.0,
              "GIB": 1024.0, "TIB": 1048576.0}

# How long two containers for one app must BOTH have been running before that is
# a duplicate rather than a deploy in progress (measured here: under 30s).
DUPLICATE_GRACE_S = 600


def _age_s(started: str | None) -> float:
    """Seconds since a Docker timestamp, or 0 if it will not parse.

    Docker writes NANOsecond precision ('…T16:12:09.620469741Z') and
    `fromisoformat` accepts only 3 or 6 fractional digits, so it is trimmed
    first. Unparseable reads as 0 — i.e. "too new to judge", which keeps an
    unreadable timestamp from manufacturing a duplicate report.
    """
    if not started:
        return 0.0
    v = re.sub(r"(\.\d{6})\d+", r"\1", started.strip().replace("Z", "+00:00"))
    try:
        at = datetime.fromisoformat(v)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - at).total_seconds()


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


def _size_to_mb(tok: str) -> float | None:
    """'1.23MB' / '0B' / '4.5kB' (docker ps -s uses SI units here) -> megabytes."""
    m = re.match(r"([0-9.]+)\s*([A-Za-z]*)", tok.strip())
    if not m:
        return None
    units = {"B": 1 / 1e6, "KB": 1 / 1e3, "MB": 1.0, "GB": 1e3, "TB": 1e6,
             "": 1 / 1e6}
    factor = units.get(m.group(2).upper())
    return round(float(m.group(1)) * factor, 1) if factor is not None else None


async def _disk_sizes() -> dict[str, dict]:
    """name -> {disk_rw_mb, image_mb}. `docker ps -s` reports Size as
    '<writable> (virtual <total-incl-image>)'; the writable layer is what the
    running container has written on top of its image."""
    _, out = await autoupdate._docker(
        "ps", "-a", "-s", "--no-trunc", "--format", "{{.Names}}\t{{.Size}}",
        timeout=60)
    res: dict[str, dict] = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) < 2:
            continue
        m = re.match(r"\s*([^(]+?)\s*(?:\(virtual\s+([^)]+)\))?\s*$", p[1])
        if not m:
            continue
        res[p[0]] = {"disk_rw_mb": _size_to_mb(m.group(1)),
                     "image_mb": _size_to_mb(m.group(2)) if m.group(2) else None}
    return res


async def _host(stats: dict[str, dict]) -> dict:
    """Host capacity (CPUs, total RAM, disk) plus what containers use right now."""
    _, out = await autoupdate._docker(
        "info", "--format", "{{.NCPU}} {{.MemTotal}}", timeout=20)
    ncpu = mem_total = None
    parts = out.split()
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        ncpu = int(parts[0])
        mem_total = round(int(parts[1]) / 1048576, 1)
    used = round(sum(v["mem_mb"] for v in stats.values() if v.get("mem_mb")), 1)

    host = {"ncpu": ncpu, "mem_total_mb": mem_total,
            "mem_used_by_containers_mb": used}
    # Disk: the container root sits on the host's docker partition, so statvfs of
    # '/' reflects the Pi's underlying filesystem size and free space.
    try:
        du = shutil.disk_usage("/")
        host.update({
            "disk_total_gb": round(du.total / 1e9, 1),
            "disk_used_gb": round(du.used / 1e9, 1),
            "disk_free_gb": round(du.free / 1e9, 1),
            "disk_used_pct": round(100 * du.used / du.total) if du.total else None,
        })
    except OSError:
        pass
    return host


def _percentile_from_hist(counts: list[int], p: float) -> float | None:
    """Estimate the p-th percentile (p in 0..1) from histogram bucket counts,
    reported as the upper edge of the bucket the percentile falls in. Conservative
    (rounds up to the edge); the overflow bucket reports the last edge as a floor."""
    total = sum(counts)
    if total == 0:
        return None
    target = p * total
    cum = 0
    for i, c in enumerate(counts):
        cum += c
        if cum >= target:
            return float(LATENCY_BUCKETS_MS[i]) if i < len(LATENCY_BUCKETS_MS) \
                else float(LATENCY_BUCKETS_MS[-1])
    return float(LATENCY_BUCKETS_MS[-1])


async def _latency_map(conn, days: int) -> dict[str, dict]:
    """app_id -> {p50_ms, p95_ms}, estimated from the latency histogram."""
    rows = await conn.fetch(
        "SELECT app_id, bucket, SUM(count) c FROM analytics_latency "
        "WHERE day > CURRENT_DATE - $1::int GROUP BY app_id, bucket", days)
    nbuckets = len(LATENCY_BUCKETS_MS) + 1
    hist: dict[str, list[int]] = {}
    for r in rows:
        h = hist.setdefault(r["app_id"], [0] * nbuckets)
        if 0 <= r["bucket"] < nbuckets:
            h[r["bucket"]] = r["c"]
    return {app: {"p50_ms": _percentile_from_hist(h, 0.50),
                  "p95_ms": _percentile_from_hist(h, 0.95)}
            for app, h in hist.items()}


async def _last_seen_map(conn) -> dict[str, str]:
    """app_id -> ISO timestamp of its most recent request. Not windowed like the
    rollups: an app nobody has touched in months is exactly the one whose last
    access is worth knowing."""
    rows = await conn.fetch("SELECT app_id, last_seen FROM analytics_last_seen")
    return {r["app_id"]: r["last_seen"].isoformat() for r in rows if r["last_seen"]}


async def last_seen_of(app_id: str) -> str | None:
    """When one app was last requested, or None if it never has been."""
    async with pool().acquire() as conn:
        at = await conn.fetchval(
            "SELECT last_seen FROM analytics_last_seen WHERE app_id = $1", app_id)
    return at.isoformat() if at else None


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
            "p50_ms": None, "p95_ms": None,  # filled from the latency histogram
        }
    return out


async def _lifecycle() -> dict[str, dict]:
    """name -> {state, since} for EVERY container, running or not.

    Deliberately not derived from _container_stats, which lists only running
    containers — the stopped ones are exactly the interesting case here. A
    sleeping app's container still exists; it simply is not up, and Docker
    records when it went down.

    `since` is StartedAt for a running container and FinishedAt for a stopped
    one, i.e. when the state now being reported actually began. Two calls, not
    one per container: list the names, then inspect them all at once.
    """
    _, names_out = await autoupdate._docker(
        "ps", "-a", "--format", "{{.Names}}", timeout=30)
    names = [n for n in names_out.splitlines() if n.strip()]
    if not names:
        return {}
    _, out = await autoupdate._docker(
        "inspect", *names, "--format",
        "{{.Name}}\t{{.State.Status}}\t{{.State.StartedAt}}\t{{.State.FinishedAt}}",
        timeout=40)
    res: dict[str, dict] = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) != 4:
            continue
        name, status, started, finished = p[0].lstrip("/"), p[1], p[2], p[3]
        since = started if status == "running" else finished
        # Docker writes a zero time for a container that has never stopped;
        # reporting 0001-01-01 as "asleep since" would be worse than saying
        # nothing, so drop it.
        res[name] = {"state": status,
                     "since": None if since.startswith("0001-01-01") else since}
    return res


async def app_runtime(uuid: str | None, sleeps: bool = False) -> dict:
    """Live state for ONE app: is its container up, and what is it using right now.

    One `docker stats` sample, so it costs about a second — fine for a per-app
    lookup. `state` distinguishes 'asleep' (stopped by scale-to-zero, wakes on
    the next request) from 'stopped' (not running and not supposed to be), which
    matters because the first is healthy and the second is not.
    """
    if not uuid:
        return {"state": "static", "container": None,
                "note": "no container — served by the shared frontend host"}
    stats = await _container_stats()
    cname = next((n for n in stats if uuid in n), None)
    if not cname:
        # `_container_stats` sees running containers only, so "not here" covers
        # both a slept container and a deleted one — and they need opposite
        # responses. Only a container that EXISTS can be woken by a request; a
        # deleted one has to be redeployed. Ask docker for the stopped ones too.
        if not await autoupdate._container_name(uuid):
            return {"state": "missing", "container": None,
                    "note": ("no container exists at all — a sleeping app whose "
                             "container was deleted cannot wake on the next "
                             "request and has to be redeployed" if sleeps else
                             "not running, and there is no container to start — "
                             "the deploy failed or was rolled back. apps_logs "
                             "shows why")}
        return {"state": "asleep" if sleeps else "stopped", "container": None,
                "note": ("stopped while idle; the next request wakes it"
                         if sleeps else
                         "not running — the deploy failed, was rolled back, or it "
                         "crashed. apps_logs shows why")}
    s = stats[cname]
    # Ground truth from Docker, not what we asked Coolify for: 'unless-stopped'
    # is what keeps a slept app from waking on reboot, so report what is actually
    # in force rather than leaving it to be inferred.
    _, policy = await autoupdate._docker(
        "inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", cname,
        timeout=20)
    return {"state": "running", "container": cname,
            "cpu_pct": s.get("cpu_pct"), "mem_mb": s.get("mem_mb"),
            "mem_pct": s.get("mem_pct"),
            "restart_policy": policy.strip() or None}


async def app_resources(include_db: bool = True, perf_days: int = 7) -> dict:
    """Per-app resource snapshot plus host totals. `include_db=False` skips the
    (slightly slower) database-size probe for a faster CPU/RAM-only view."""
    async with pool().acquire() as conn:
        apps = await conn.fetch(
            "SELECT id, coolify_uuid, db_engine, db_user, db_password, db_name, "
            "has_frontend, sleep_when_idle FROM apps ORDER BY id")
        perf = await _perf_map(conn, perf_days)
        latency = await _latency_map(conn, perf_days)
        last_seen = await _last_seen_map(conn)
    for aid, lat in latency.items():
        if aid in perf:
            perf[aid].update(lat)

    stats = await _container_stats()
    disk = await _disk_sizes()
    life = await _lifecycle()
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
        live = [n for n in names if uuid and uuid in n]
        cname = live[0] if live else None
        # A deploy runs two containers for a few seconds — Coolify starts the new
        # one and stops the old once it is healthy — so only containers that have
        # both been up a while are a duplicate rather than a deploy in flight.
        # `life` is already fetched for state_since, so this costs no extra call.
        if len(live) > 1:
            live = [n for n in live
                    if _age_s(life.get(n, {}).get("since")) > DUPLICATE_GRACE_S]
        # `names` holds RUNNING containers only, so its absence cannot tell a
        # slept container from a deleted one. `disk` is built from `docker ps -a`,
        # so its keys answer that for free — no extra call.
        exists = next((n for n in disk if uuid and uuid in n), None)
        s = stats.get(cname, {})
        d = disk.get(cname, {})
        has_backend = bool(app["coolify_uuid"])
        return {
            "id": app["id"],
            # A frontend-only app has no container of its own, so "running" is not
            # a meaningful state for it — its assets are served by the static host.
            "kind": ("both" if has_backend and app["has_frontend"]
                     else "frontend" if app["has_frontend"]
                     else "backend"),
            "running": (cname is not None) if has_backend else None,
            # Distinguishes a stopped-because-idle app from a broken one. 'asleep'
            # is only true while a container still EXISTS to be started: one that
            # has been deleted cannot be woken by a request, and reporting that as
            # asleep describes a dead app as a healthy one.
            "state": ("static" if not has_backend
                      else "running" if cname
                      else "missing" if not exists
                      else "asleep" if app["sleep_when_idle"]
                      else "stopped"),
            # An app is supposed to have exactly one container. When it has two,
            # both carry the same Coolify-generated Traefik router and service,
            # so Traefik round-robins between them and the older one answers with
            # whatever image it was built from — a wrong-version outage that
            # every other field here reports as healthy. Null unless it happens,
            # so it costs a reader nothing until it means something.
            "duplicate_containers": live if len(live) > 1 else None,
            "cpu_pct": s.get("cpu_pct"),
            "mem_mb": s.get("mem_mb"),
            "mem_pct": s.get("mem_pct"),
            "disk_rw_mb": d.get("disk_rw_mb"),
            "image_mb": d.get("image_mb"),
            "db_engine": app["db_engine"],
            "db_size_mb": await db_mb(app),
            # Null means "never seen in the access log" — a brand-new app, or one
            # that has had no traffic since ingestion started.
            # When the state above began — StartedAt if running, FinishedAt if
            # not. Distinct from last_seen on purpose: for an app with a frontend
            # AND a backend, last_seen is edge traffic, which the static bundle
            # answers from the CDN without waking anything. So "accessed 2 hours
            # ago" and "backend asleep since yesterday" are both true at once,
            # and only this field says the second.
            # The static host is infrastructure, not a destination: it serves
            # OTHER apps' hostnames, and analytics credits a request to the host
            # header it carried. So its own last_seen is structurally always
            # empty while it handles every request on the box — "never" there is
            # true and says the opposite of the truth. Marked so a reader can
            # decline to show a number that cannot mean anything.
            "role": "static-host" if app["id"] == STATIC_HOST_APP else None,
            "state_since": (next((v["since"] for n, v in life.items()
                                  if uuid and uuid in n), None)
                            if has_backend else None),
            "last_seen": last_seen.get(app["id"]),
            "traffic": perf.get(app["id"], {
                "requests": 0, "error_pct": 0, "server_error_pct": 0,
                "avg_ms": None, "p50_ms": None, "p95_ms": None}),
        }

    rows = await asyncio.gather(*[one(a) for a in apps])
    return {"apps": list(rows), "host": await _host(stats),
            "traffic_window_days": perf_days}
