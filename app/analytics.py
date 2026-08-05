"""Cross-app analytics from the shared Traefik access log.

paas-api is not in any other app's request path, so per-app instrumentation
would mean touching every app. Instead we read the one place all app traffic
already passes through: the edge proxy (Traefik) that Coolify runs. paas-api
tails that container's access log over the mounted docker socket, turns each
request into a cookieless visitor id, and rolls it up into small per-app/day
tables. Nothing is installed in the apps; a new app is measured the moment it
starts serving.

Privacy: a **visitor** is sha256(salt | ip | user-agent), truncated — no cookie,
no raw IP stored. The app_id is deliberately NOT in the hash, so the same person
hitting two apps is one visitor (that is what makes cross-app unique counts and
global cohorts meaningful). Behind Cloudflare the real client IP arrives in the
CF-Connecting-Ip header, so the access log must be configured to keep it (and
User-Agent) — see the one-time proxy setup in the README.

The one exception is recent_requests: a live tail of individual requests, read
straight off the proxy log and never written anywhere, which reports the client
IP as-is. Without it a tail cannot answer the question it exists for — who is
hammering this app — since a truncated hash is not something you can block or
look up. It stays a read of the log rather than a stored column: the rollups
above still keep no raw IP, and the endpoint is behind the same API key as the
rest of the platform.

Ingestion is idempotent via a StartUTC cursor in analytics_state: each pass reads
only log lines newer than the last one counted, so overlapping `docker logs`
windows never double-count.
"""
import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone

from . import autoupdate
from .config import ANALYTICS_PROXY, ANALYTICS_SALT, DOMAIN_SUFFIX
from .db import pool

_log = logging.getLogger("pironman.analytics")

# How far the cursor may fall behind before a pass that counted nothing is
# treated as a stall rather than a quiet box.
_STALL_AFTER = 15 * 60

# How far behind the cursor may be before a catch-up is worth mentioning.
MAX_WINDOW = 60 * 60

# The most log we will ask Docker for in one pass, in LINES — not a time window,
# and deliberately MODEST. Asking for too many is not merely wasteful here: past
# some threshold Docker stops tailing from the end and returns truncated output
# from the START of the log instead, i.e. the oldest lines. See _read_since.
# Measured good up to 3000 on this box; 2000 leaves margin and is still hours of
# traffic, against a 120s pass.
MAX_LINES = 2000


_CURSOR_KEY = "accesslog_cursor"

# Upper edges (ms) of the latency histogram buckets; a request lands in the first
# bucket whose edge it does not exceed, and anything slower goes in the overflow
# bucket (index == len(LATENCY_BUCKETS_MS)). Additive counts per (app, day,
# bucket) let p50/p95 be estimated over any window — see stats._latency_map.
LATENCY_BUCKETS_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]


def _bucket(dur_ms: float) -> int:
    for i, edge in enumerate(LATENCY_BUCKETS_MS):
        if dur_ms <= edge:
            return i
    return len(LATENCY_BUCKETS_MS)  # overflow: slower than the last edge


# Coarse "is this a non-human client?" test on the user-agent. Not exhaustive —
# it catches the common crawlers, monitors and scripting clients (and treats an
# empty UA as a bot, since browsers always send one) so the humans-vs-bots split
# is directionally right, not a security control.
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python-|java/|go-http|okhttp|libwww|"
    r"httpclient|headless|phantom|lighthouse|monitor|uptime|pingdom|statuscake|"
    r"semrush|ahrefs|mj12|dotbot|facebookexternalhit|embedly|preview|scan|"
    r"fetch|feed|rss|http_request|axios|node-fetch|postman",
    re.IGNORECASE)


def _is_bot(ua: str) -> bool:
    return (not ua) or bool(_BOT_RE.search(ua))


# Traefik stamps StartUTC with nanosecond precision, which fromisoformat rejects;
# trim the fraction to the six digits a datetime can hold.
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _moment(start: str) -> datetime | None:
    """An RFC3339 StartUTC as an aware datetime, or None if it will not parse."""
    try:
        return datetime.fromisoformat(_FRACTION.sub(r"\1", start).replace("Z", "+00:00"))
    except ValueError:
        return None


def _behind(cursor: str | None) -> float | None:
    """Seconds between the cursor and now, or None if it will not parse."""
    if not cursor:
        return 0.0
    at = _moment(cursor)
    return None if at is None else (datetime.now(timezone.utc) - at).total_seconds()


def _client_ip(e: dict) -> str:
    """The real client IP for one access-log entry, or "" if the log has none.

    ClientHost alone is Cloudflare's edge (or the proxy's own docker-network peer
    for internally-forwarded requests), so it is the last resort, not the first:
    CF-Connecting-Ip is what Cloudflare puts the originating address in, and
    X-Forwarded-For's first element covers anything arriving through some other
    forwarder.
    """
    return (e.get("request_Cf-Connecting-Ip")
            or (e.get("request_X-Forwarded-For") or "").split(",")[0]
            or e.get("ClientHost") or "").strip()


def _visitor(ip: str, ua: str) -> str:
    """Cookieless, stable-per-person id. Excludes app_id on purpose: the same
    (ip, ua) is one visitor across every app."""
    h = hashlib.sha256(f"{ANALYTICS_SALT}|{ip}|{ua}".encode()).hexdigest()
    return h[:32]


def _parse_line(line: str) -> dict | None:
    """One JSON access-log line -> {app_id, visitor, day, start} or None.

    Returns None for anything that is not a request to a hosted app: non-JSON
    lines (Traefik startup/error logs are plain text), hosts that are not
    <app>-coolify.bogdanripa.com, and requests with no usable client identity.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None

    host = (e.get("RequestHost") or "").strip().lower()
    if not host.endswith(DOMAIN_SUFFIX):
        return None
    app_id = host[: -len(DOMAIN_SUFFIX)]
    if not app_id or "." in app_id:  # only flat <app> labels are real apps
        return None

    ip = _client_ip(e)
    ua = (e.get("request_User-Agent") or "").strip()
    if not ip and not ua:
        return None

    start = (e.get("StartUTC") or "").strip()
    if len(start) < 10:
        return None
    try:
        day = date.fromisoformat(start[:10])  # date column wants a date, not str
    except ValueError:
        return None

    status = e.get("DownstreamStatus")
    dur = e.get("Duration")  # Traefik reports request time in nanoseconds
    return {"app_id": app_id, "visitor": _visitor(ip, ua),
            "day": day, "start": start, "at": _moment(start), "is_bot": _is_bot(ua),
            "ua": (ua or "(none)")[:512],  # stored for the top-agents breakdown
            "status": int(status) if isinstance(status, (int, float)) else None,
            "dur_ms": (dur / 1e6) if isinstance(dur, (int, float)) else None}


async def _read_since(cursor: str | None) -> str:
    """Raw access-log text from the proxy container, bounded by LINE COUNT.

    **Not `--since`, and not a large `--tail` either.** Both make Docker read
    from the START of the log, and on this box that read is truncated long before
    it reaches recent entries — so it returns old lines, or none, which is
    indistinguishable from a quiet log. Measured against the live proxy
    container (Docker 29.6.2, json-file):

        --tail 3 .. 3000              -> current, newest 2026-08-01T07:47:19Z
        --tail 20000                  -> 2333 lines, NONE from Aug 1,
                                         newest 2026-07-31T07:09:33Z
        --since 2026-07-01T00:00:00Z  -> 2333 lines (same truncated prefix)
        --since 1h                    -> 0
        --until now                   -> 0        <- should be everything

    The `--since` results are the same failure seen from a different angle: the
    filter is applied to a prefix that stops before the recent lines, so any
    recent window matches nothing. `--until now` returning nothing is the tell —
    it is not the window that is wrong.

    A bounded `--tail` reads backwards from the end and is exact. There is
    corroboration in this module: recent() has always used a small `--tail` and
    has always worked, against the same container through the same socket, while
    ingestion read from the start and sat frozen for a day.

    Correctness does not depend on the window anyway. Every line is checked
    against the cursor by StartUTC in ingest_once, so re-reading is free and only
    under-reading loses data. MAX_LINES is therefore generous: it needs to exceed
    the traffic between two passes (120s), and the whole log is currently 2333
    lines.
    """
    rc, out = await autoupdate._docker(
        "logs", "--tail", str(MAX_LINES), ANALYTICS_PROXY, timeout=120)
    if rc != 0:
        # _docker reports a timeout by RETURNING (124, "docker timed out").
        # Ignoring that yields a string with no JSON in it, which reads exactly
        # like a quiet log — zero counted, no error, cursor pinned.
        _log.warning("analytics: could not read %s logs (rc=%s): %s",
                     ANALYTICS_PROXY, rc, out.strip()[:200])
        return ""
    behind = _behind(cursor)
    if behind is not None and behind > MAX_WINDOW:
        _log.warning("analytics: cursor was %.0f minutes behind; catching up "
                     "from the last %d log lines", behind / 60, MAX_LINES)
    return out


def _diagnose(t: dict) -> str:
    """Turn the tally into the thing to go and check."""
    if not t["lines"]:
        return (f"The {ANALYTICS_PROXY} container produced no log output at all.")
    if not t["json"]:
        return ("None of it is JSON, so Traefik is not writing a JSON access log. "
                "Those proxy flags are a hand-edit and Coolify regenerates them "
                "away on a proxy restart — re-apply them.")
    if not t["app"]:
        return (f"JSON is being written but no line is a request to a "
                f"*{DOMAIN_SUFFIX}* host with a usable client identity — check "
                "that the access log still keeps RequestHost, User-Agent and "
                "Cf-Connecting-Ip.")
    # Defensive only. The caller now treats "every hosted-app line was already
    # counted" as caught up and logs it at debug, so by the time _diagnose is
    # called the tally has no hosted-app lines at all — and `if not t["app"]`
    # above has already returned. Anything reaching here contradicts that, so
    # say so rather than inventing a reassuring explanation for it.
    return ("Nothing counted, and none of the checks above explains why — the "
            "tally is not a shape this should be able to produce.")


def _stamp_of(line: str) -> str:
    """StartUTC from any JSON access-log line, whether or not it is a request to
    a hosted app. Used only to move the cursor.

    The distinction matters: the proxy logs plenty that _parse_line rightly
    rejects — bots hitting the box's bare IP via catchall@file, requests to
    hostnames this platform does not own. Those are not traffic to any app, but
    they ARE proof that time has passed and the window up to here has been read.
    """
    line = line.strip()
    if not line.startswith("{"):
        return ""
    try:
        e = json.loads(line)
    except ValueError:
        return ""
    s = (e.get("StartUTC") or "").strip()
    return s if len(s) >= 10 else ""


async def ingest_once() -> dict:
    """Fold new access-log lines into the rollup tables. Idempotent: only lines
    with StartUTC strictly greater than the stored cursor are counted, and the
    cursor advances to the newest line seen. Safe to call on a short timer."""
    async with pool().acquire() as conn:
        cursor = await conn.fetchval(
            "SELECT v FROM analytics_state WHERE k = $1", _CURSOR_KEY)

        raw = await _read_since(cursor)

        # Aggregate the batch in memory so each (app, visitor, day) is one upsert.
        visits: dict[tuple[str, str, str], int] = {}
        bot: dict[tuple[str, str, str], bool] = {}  # (app, visitor, day) -> is_bot
        first: dict[tuple[str, str], str] = {}
        perf: dict[tuple[str, str], list] = {}  # (app, day) -> [req, 4xx, 5xx, ms]
        lat: dict[tuple[str, str, int], int] = {}  # (app, day, bucket) -> count
        agents: dict[tuple[str, str, str], list] = {}  # (app, day, ua) -> [hits, bot]
        last: dict[str, datetime] = {}  # app -> newest request time in this batch
        # Fallback for a line whose timestamp will not parse. "Last accessed" is
        # answering "has anyone used this lately", and the ingest pass is at most
        # two minutes behind the request — so being approximately right beats
        # recording nothing, which is what an unparsed stamp used to mean.
        now = datetime.now(timezone.utc)
        newest = cursor or ""
        # How far the READ got, as opposed to how far the counting got. The
        # cursor used to advance only on lines belonging to a hosted app, so a
        # window containing nothing but bot hits on the bare IP left it exactly
        # where it was — and the next pass re-read the same span, found the same
        # nothing, and moved nothing again. On a quiet box that span only grows,
        # and the stall is invisible: every pass "succeeds" having counted zero.
        horizon = cursor or ""
        # The newest stamp actually present in this read, tracked separately from
        # `horizon` — which starts AT the cursor and so can never fall below it.
        # Only an independent value can show the read handing back older data
        # than we already have, which is the truncation signature.
        newest_line = ""
        seen = 0
        # Where lines are lost, so "counted nothing" can name its own cause.
        # Each stage is a different fault: no lines at all means the proxy log is
        # unreadable; lines but no JSON means Traefik is not writing a JSON access
        # log (the one-time proxy flags are a hand-edit, and Coolify regenerates
        # what Coolify generates); JSON but no app hosts means the log is missing
        # RequestHost or the domain suffix changed; and everything older than the
        # cursor is the only case that is actually normal.
        tally = {"lines": 0, "json": 0, "app": 0, "old": 0}
        for line in raw.splitlines():
            stamp = _stamp_of(line)
            if stamp > horizon:
                horizon = stamp
            if stamp > newest_line:
                newest_line = stamp
            tally["lines"] += 1
            if line.strip().startswith("{"):
                tally["json"] += 1
            rec = _parse_line(line)
            if rec is None:
                continue
            tally["app"] += 1
            if cursor and rec["start"] <= cursor:
                tally["old"] += 1
                continue  # already counted in an earlier pass
            seen += 1
            if rec["start"] > newest:
                newest = rec["start"]
            k = (rec["app_id"], rec["visitor"], rec["day"])
            visits[k] = visits.get(k, 0) + 1
            bot[k] = rec["is_bot"]
            fk = (rec["app_id"], rec["visitor"])
            if fk not in first or rec["day"] < first[fk]:
                first[fk] = rec["day"]

            pr = perf.setdefault((rec["app_id"], rec["day"]), [0, 0, 0, 0.0])
            pr[0] += 1
            st = rec["status"]
            if st is not None and 400 <= st < 500:
                pr[1] += 1
            elif st is not None and st >= 500:
                pr[2] += 1
            if rec["dur_ms"] is not None:
                pr[3] += rec["dur_ms"]
                lk = (rec["app_id"], rec["day"], _bucket(rec["dur_ms"]))
                lat[lk] = lat.get(lk, 0) + 1

            ak = (rec["app_id"], rec["day"], rec["ua"])
            ar = agents.setdefault(ak, [0, rec["is_bot"]])
            ar[0] += 1

            at = rec["at"] or now
            if rec["app_id"] not in last or at > last[rec["app_id"]]:
                last[rec["app_id"]] = at

        if visits:
            await conn.executemany(
                "INSERT INTO analytics_visits (app_id, visitor, day, hits, is_bot) "
                "VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (app_id, visitor, day) "
                "DO UPDATE SET hits = analytics_visits.hits + EXCLUDED.hits",
                [(a, v, d, h, bot[(a, v, d)]) for (a, v, d), h in visits.items()])
            await conn.executemany(
                "INSERT INTO analytics_first_seen (app_id, visitor, first_day) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (app_id, visitor) "
                "DO UPDATE SET first_day = LEAST(analytics_first_seen.first_day, "
                "EXCLUDED.first_day)",
                [(a, v, d) for (a, v), d in first.items()])
            await conn.executemany(
                "INSERT INTO analytics_perf "
                "(app_id, day, requests, err_client, err_server, dur_ms_sum) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (app_id, day) DO UPDATE SET "
                "requests   = analytics_perf.requests   + EXCLUDED.requests, "
                "err_client = analytics_perf.err_client + EXCLUDED.err_client, "
                "err_server = analytics_perf.err_server + EXCLUDED.err_server, "
                "dur_ms_sum = analytics_perf.dur_ms_sum + EXCLUDED.dur_ms_sum",
                [(a, d, v[0], v[1], v[2], round(v[3]))
                 for (a, d), v in perf.items()])
            await conn.executemany(
                "INSERT INTO analytics_latency (app_id, day, bucket, count) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (app_id, day, bucket) "
                "DO UPDATE SET count = analytics_latency.count + EXCLUDED.count",
                [(a, d, b, c) for (a, d, b), c in lat.items()])
            await conn.executemany(
                "INSERT INTO analytics_agents (app_id, day, ua, is_bot, hits) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (app_id, day, ua) "
                "DO UPDATE SET hits = analytics_agents.hits + EXCLUDED.hits",
                [(a, d, ua, v[1], v[0]) for (a, d, ua), v in agents.items()])
            # GREATEST, not assignment: a pass could otherwise move an app's
            # last-seen backwards if its lines arrive out of order.
            #
            # Isolated on purpose. The rollups above are already committed by the
            # time this runs, and the cursor advances after it — so letting a
            # failure here escape would leave the cursor unmoved and every later
            # pass would re-count the same window, inflating every number on the
            # dashboard. This one column is not worth that.
            try:
                await conn.executemany(
                    "INSERT INTO analytics_last_seen (app_id, last_seen) "
                    "VALUES ($1, $2) ON CONFLICT (app_id) DO UPDATE SET last_seen = "
                    "GREATEST(analytics_last_seen.last_seen, EXCLUDED.last_seen)",
                    list(last.items()))
            except Exception:
                _log.exception("could not record last-accessed times")

        # Advance to the furthest point actually read, not merely the newest line
        # that happened to belong to an app. Everything up to here has been
        # examined, so re-reading it can only find what was already counted.
        mark = max(newest, horizon)
        if mark and mark != cursor:
            await conn.execute(
                "INSERT INTO analytics_state (k, v) VALUES ($1, $2) "
                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                _CURSOR_KEY, mark)

    # Say what happened. A pass that counts nothing is normal on a quiet box and
    # indistinguishable from one that is stuck — which is exactly the state that
    # left "last accessed" empty with no error anywhere. So the quiet case is
    # silent only while the cursor is keeping up; once it falls behind, that is a
    # stall and it says so.
    if seen:
        _log.info("analytics: counted %d lines, cursor now %s", seen, mark)
    else:
        behind = _behind(cursor)
        if behind is None:
            _log.warning("analytics: no lines counted and the cursor %r cannot be "
                         "parsed — nothing will ever advance", cursor)
        elif newest_line and cursor and newest_line < cursor:
            # The NEWEST line we were handed is older than the cursor. That
            # cannot happen on a healthy tail — being caught up means the newest
            # line equals the cursor, never precedes it — so the read returned
            # the START of the log instead of the end. Lower MAX_LINES.
            #
            # Note the condition, which two attempts have now got wrong. It is
            # strictly OLDER, and neither "the window was full and nothing in it
            # was newer" nor "nothing newer for a long time" is that. The proxy
            # log is permanently longer than MAX_LINES, so every window is full;
            # and an idle box legitimately produces nothing newer for hours. The
            # first version fired five times against a healthy ingester; the
            # second widened it to `<=` gated on _STALL_AFTER and would have
            # fired on every idle stretch over fifteen minutes.
            _log.warning(
                "analytics: the newest line read (%s) is OLDER than the cursor "
                "(%s) — the read is returning the start of the log, not the "
                "end. Lower MAX_LINES.", newest_line, cursor)
        elif (newest_line and cursor and newest_line <= cursor) or (
                tally["app"] and tally["app"] == tally["old"]):
            # Caught up. There is no fault here to report: the cursor is old
            # because the BOX is idle, not because ingestion is stuck. Saying
            # anything louder is what the code before this got wrong — it
            # warned "the cursor is 35 minutes behind", which measures how long
            # since the last request, not how long since ingestion last worked.
            # On a personal box that can be hours, and the alarm cost an hour of
            # chasing a rotation bug that was not there.
            #
            # Two ways to be sure, and the SECOND is the one that fires on a box
            # facing the open internet. The first — nothing newer than the
            # cursor at all — is very nearly unreachable, because the cursor
            # advances to the newest line of ANY kind, bot hits on the bare IP
            # included. So a quiet stretch ended by a single scanner request
            # leaves newest_line ahead of the cursor and drops through to the
            # warning below, which then calls a merely idle box a stall. That is
            # the same misreading arriving by the other branch, and it was
            # emitting ~30 warnings a day here while ingestion was provably
            # exact against the proxy log.
            #
            # The second test names the real question: every hosted-app line in
            # the window had already been counted, so nothing of ours was
            # missed. What broke the silence belonged to no app of ours.
            _log.debug("analytics: caught up at %s; nothing new belonging to a "
                       "hosted app since (%.0f minutes)", cursor, behind / 60)
        elif behind > _STALL_AFTER:
            _log.warning(
                "analytics: nothing counted and the cursor is %.0f minutes behind "
                "(%s). Read %d log lines, %d were JSON, %d were requests to a "
                "hosted app, %d of those older than the cursor. %s",
                behind / 60, cursor, tally["lines"], tally["json"], tally["app"],
                tally["old"], _diagnose(tally))
    return {"lines_counted": seen, "cursor": mark or None}


async def overview(app_id: str | None = None, days: int = 30) -> dict:
    """Headline numbers: unique visitors + hits over the window, plus DAU/WAU/MAU
    (distinct visitors in the last 1 / 7 / 30 days) and a humans-vs-bots split.
    With no app_id, also returns a per-app breakdown so you can see every app at a
    glance."""
    # One app filters on app_id ($1) and windows on $2; global windows on $1.
    app_clause = " AND app_id = $1" if app_id else ""
    win = "$2" if app_id else "$1"

    async with pool().acquire() as conn:
        async def distinct(window: int) -> int:
            args = [app_id, window] if app_id else [window]
            return await conn.fetchval(
                "SELECT COUNT(DISTINCT visitor) FROM analytics_visits "
                f"WHERE day > CURRENT_DATE - {win}::int" + app_clause, *args) or 0

        args = [app_id, days] if app_id else [days]
        totals = await conn.fetchrow(
            "SELECT COALESCE(COUNT(DISTINCT visitor), 0) AS visitors, "
            "       COALESCE(SUM(hits), 0) AS hits FROM analytics_visits "
            f"WHERE day > CURRENT_DATE - {win}::int" + app_clause, *args)

        result = {
            "scope": app_id or "all apps",
            "window_days": days,
            "unique_visitors": totals["visitors"],
            "hits": totals["hits"],
            "dau": await distinct(1),
            "wau": await distinct(7),
            "mau": await distinct(30),
        }
        agent_rows = await conn.fetch(
            "SELECT is_bot, COUNT(DISTINCT visitor) AS visitors, SUM(hits) AS hits "
            "FROM analytics_visits "
            f"WHERE day > CURRENT_DATE - {win}::int" + app_clause +
            " GROUP BY is_bot", *args)
        by = {False: {"visitors": 0, "hits": 0}, True: {"visitors": 0, "hits": 0}}
        for r in agent_rows:
            by[r["is_bot"]] = {"visitors": r["visitors"], "hits": r["hits"] or 0}
        result["by_agent"] = {"human": by[False], "bot": by[True]}

        if not app_id:
            rows = await conn.fetch(
                "SELECT app_id, COUNT(DISTINCT visitor) AS visitors, "
                "       SUM(hits) AS hits FROM analytics_visits "
                "WHERE day > CURRENT_DATE - $1::int "
                "GROUP BY app_id ORDER BY visitors DESC, hits DESC", days)
            result["per_app"] = [
                {"app_id": r["app_id"], "unique_visitors": r["visitors"],
                 "hits": r["hits"]} for r in rows]
        return result


async def timeseries(app_id: str | None = None, days: int = 30) -> dict:
    """Daily unique visitors and hits over the window — the shape for a line
    chart. Days with no traffic are simply absent from the list."""
    async with pool().acquire() as conn:
        if app_id:
            rows = await conn.fetch(
                "SELECT day, COUNT(DISTINCT visitor) AS visitors, SUM(hits) AS hits "
                "FROM analytics_visits "
                "WHERE app_id = $1 AND day > CURRENT_DATE - $2::int "
                "GROUP BY day ORDER BY day", app_id, days)
        else:
            rows = await conn.fetch(
                "SELECT day, COUNT(DISTINCT visitor) AS visitors, SUM(hits) AS hits "
                "FROM analytics_visits WHERE day > CURRENT_DATE - $1::int "
                "GROUP BY day ORDER BY day", days)
        return {
            "scope": app_id or "all apps",
            "window_days": days,
            "series": [
                {"day": r["day"].isoformat(), "unique_visitors": r["visitors"],
                 "hits": r["hits"]} for r in rows],
        }


async def top_agents(app_id: str | None = None, days: int = 30,
                     limit: int = 20) -> dict:
    """The most-seen user-agent strings over the window, with hit counts and the
    bot flag — so you can see exactly which browsers, crawlers and scripts are
    hitting an app, not just the humans-vs-bots summary."""
    async with pool().acquire() as conn:
        if app_id:
            rows = await conn.fetch(
                "SELECT ua, bool_or(is_bot) AS is_bot, SUM(hits) AS hits "
                "FROM analytics_agents "
                "WHERE app_id = $1 AND day > CURRENT_DATE - $2::int "
                "GROUP BY ua ORDER BY hits DESC LIMIT $3", app_id, days, limit)
        else:
            rows = await conn.fetch(
                "SELECT ua, bool_or(is_bot) AS is_bot, SUM(hits) AS hits "
                "FROM analytics_agents WHERE day > CURRENT_DATE - $1::int "
                "GROUP BY ua ORDER BY hits DESC LIMIT $2", days, limit)
    return {"scope": app_id or "all apps", "window_days": days,
            "agents": [{"ua": r["ua"], "is_bot": r["is_bot"], "hits": r["hits"]}
                       for r in rows]}


async def recent_requests(app_id: str | None = None, limit: int = 50) -> dict:
    """The most recent individual HTTP requests, newest first, read live from the
    edge proxy log (not the rollups): time, app, client IP, method, path, status
    and how long the request took. A tail, not history — only what is still in
    the proxy's log buffer.

    `dur_ms` is the proxy's own measure of the whole exchange, so for the first
    request to a sleeping app it includes the wake, which is seconds rather than
    milliseconds. That is the point: the rollups only carry a per-day average,
    which buries exactly the outlier worth looking at.

    The IP is the real client address (CF-Connecting-Ip behind Cloudflare), not
    the hashed visitor id the rollups store — see the module docstring."""
    limit = max(1, min(limit, 200))
    _, out = await autoupdate._docker(
        "logs", "--tail", str(limit * 8 + 200), ANALYTICS_PROXY, timeout=60)
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        host = (e.get("RequestHost") or "").strip().lower()
        if not host.endswith(DOMAIN_SUFFIX):
            continue
        aid = host[: -len(DOMAIN_SUFFIX)]
        if not aid or "." in aid or (app_id and aid != app_id):
            continue
        # Traefik reports request time in NANOseconds, as _parse_line also has
        # to remember. Rounded to 0.1ms: the raw value carries nanosecond digits
        # that are noise at this scale and only make the column hard to read.
        dur = e.get("Duration")
        items.append({"time": e.get("StartUTC"), "app": aid,
                      "ip": _client_ip(e),
                      "method": e.get("RequestMethod"),
                      "path": e.get("RequestPath"),
                      "status": e.get("DownstreamStatus"),
                      "dur_ms": (round(dur / 1e6, 1)
                                 if isinstance(dur, (int, float)) else None)})
    return {"scope": app_id or "all apps", "requests": items[-limit:][::-1]}


async def cohorts(app_id: str | None = None, weeks: int = 8) -> dict:
    """Weekly retention cohorts. Visitors are grouped by the week they were first
    seen (cohort); each row shows how many of that cohort came back in later
    weeks. offset 0 is the cohort's own week (its size); offset N is N weeks
    later. Globally, "first seen" means first seen on any app.

    Returned as a list of cohorts, each with the week, its size, and a
    retained[] array indexed by week offset.
    """
    async with pool().acquire() as conn:
        # first_seen per visitor: the app's own first day, or the earliest across
        # apps when global. date_trunc('week', ...) buckets into ISO weeks.
        if app_id:
            fs_sql = ("SELECT visitor, MIN(first_day) AS first_day "
                      "FROM analytics_first_seen WHERE app_id = $1 GROUP BY visitor")
            act_sql = ("SELECT DISTINCT visitor, day FROM analytics_visits "
                       "WHERE app_id = $1")
            args = [app_id, weeks]
            wk = "$2"
        else:
            fs_sql = ("SELECT visitor, MIN(first_day) AS first_day "
                      "FROM analytics_first_seen GROUP BY visitor")
            act_sql = "SELECT DISTINCT visitor, day FROM analytics_visits"
            args = [weeks]
            wk = "$1"

        # offset = whole weeks between the cohort's week and the activity week.
        # Both endpoints are truncated to the week start, so the day difference
        # is always a multiple of 7 and the division is exact.
        rows = await conn.fetch(
            "WITH fs AS (" + fs_sql + "), "
            "act AS (" + act_sql + ") "
            "SELECT date_trunc('week', fs.first_day)::date AS cohort, "
            "       ((date_trunc('week', act.day)::date "
            "         - date_trunc('week', fs.first_day)::date) / 7)::int "
            "         AS offset_weeks, "
            "       COUNT(DISTINCT act.visitor) AS visitors "
            "FROM fs JOIN act USING (visitor) "
            "WHERE fs.first_day > CURRENT_DATE - (" + wk + "::int * 7) "
            "GROUP BY cohort, offset_weeks ORDER BY cohort, offset_weeks",
            *args)

    grid: dict[str, dict[int, int]] = {}
    for r in rows:
        off = int(r["offset_weeks"])
        if off < 0 or off >= weeks:
            continue
        grid.setdefault(r["cohort"].isoformat(), {})[off] = r["visitors"]

    cohort_list = []
    for week in sorted(grid):
        counts = grid[week]
        size = counts.get(0, 0)
        retained = [counts.get(i, 0) for i in range(weeks)]
        cohort_list.append({
            "cohort_week": week,
            "size": size,
            "retained": retained,
            "retention_pct": [round(100 * c / size) if size else 0
                              for c in retained],
        })
    return {"scope": app_id or "all apps", "weeks": weeks, "cohorts": cohort_list}
