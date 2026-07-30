import asyncpg
from .config import PAAS_DB

_pool: asyncpg.Pool | None = None

# Tables this codebase owns, created on startup so a fresh box (or a new
# feature like env vars) needs no manual SQL on the Pi. IF NOT EXISTS keeps it
# safe against the hand-created apps/crons/api_keys tables already in place.
# No foreign key to apps(id): its constraints are not guaranteed here, so
# app_env rows are cleaned up explicitly when an app is deleted instead.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_env (
    key         text PRIMARY KEY,
    value       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app_env (
    app_id      text        NOT NULL,
    key         text        NOT NULL,
    value       text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, key)
);

-- api_keys predates this codebase. Add a per-app scope column in place: NULL is
-- an unscoped admin key (bootstrap, the connector), a value is a deploy key that
-- may only redeploy that one app (enforced in auth.require_key).
ALTER TABLE IF EXISTS api_keys ADD COLUMN IF NOT EXISTS app_id text;

-- Auto-update: watch_tag is the registry tag to poll (NULL = opted out);
-- deployed_digest is the image digest currently running, so a poll only
-- redeploys when the tag actually moved. See app/autoupdate.py.
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS watch_tag text;
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS deployed_digest text;

-- Sablier scale-to-zero (see app/sablier.py). sleep_when_idle: this app should
-- sleep when idle (default on — all but the excluded control-plane app).
-- sablier_enrolled: the Sablier labels are currently written to its Coolify
-- config, so re-enrollment is a no-op and won't trigger a redeploy loop.
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS sleep_when_idle boolean NOT NULL DEFAULT true;
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS sablier_enrolled boolean NOT NULL DEFAULT false;

-- Frontends (see app/frontends.py + web/). has_frontend: a static bundle has been
-- uploaded for this app and is served by the shared static host. backend_routes:
-- OPTIONAL path prefixes the backend owns outright — an escape hatch for the
-- cases static-first resolution guesses wrong (OAuth callbacks, downloads,
-- server-rendered pages). Empty is the normal case: resolution is automatic.
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS has_frontend boolean NOT NULL DEFAULT false;
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS backend_routes text[] NOT NULL DEFAULT '{}';

-- Analytics rollups from the Traefik access log (see app/analytics.py). visitor
-- is a cookieless hash of salt+IP+UA (person, not per-app), so distinct visitor
-- counts unique people across apps and filtering by app_id counts one app.
CREATE TABLE IF NOT EXISTS analytics_visits (
    app_id  text NOT NULL,
    visitor text NOT NULL,
    day     date NOT NULL,
    hits    int  NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, visitor, day)
);
CREATE INDEX IF NOT EXISTS analytics_visits_app_day ON analytics_visits (app_id, day);
CREATE INDEX IF NOT EXISTS analytics_visits_day ON analytics_visits (day);
-- is_bot: a coarse humans-vs-bots split derived from the user-agent at ingest
-- (the raw UA is never stored, only hashed into visitor), added in place.
ALTER TABLE IF EXISTS analytics_visits
    ADD COLUMN IF NOT EXISTS is_bot boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS analytics_first_seen (
    app_id    text NOT NULL,
    visitor   text NOT NULL,
    first_day date NOT NULL,
    PRIMARY KEY (app_id, visitor)
);
CREATE TABLE IF NOT EXISTS analytics_state (k text PRIMARY KEY, v text);

-- Alert bookkeeping (app/alerts.py): last-seen running state (with a small
-- consecutive-failure debounce so a rolling redeploy is not flagged as an
-- outage) and the last 5xx count observed, so only *new* server errors alert.
CREATE TABLE IF NOT EXISTS alert_state (
    app_id       text PRIMARY KEY,
    fail_count   int         NOT NULL DEFAULT 0,
    alerted_down boolean     NOT NULL DEFAULT false,
    err_day      date,
    err_server   bigint      NOT NULL DEFAULT 0,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Per-app/day request counts by raw user-agent string, so the actual agents
-- (browsers, crawlers, scripts) can be listed, not just the humans/bots split.
-- ua is truncated to 512 chars at ingest.
CREATE TABLE IF NOT EXISTS analytics_agents (
    app_id text    NOT NULL,
    day    date    NOT NULL,
    ua     text    NOT NULL,
    is_bot boolean NOT NULL DEFAULT false,
    hits   int     NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, day, ua)
);
CREATE INDEX IF NOT EXISTS analytics_agents_app_day ON analytics_agents (app_id, day);

-- Per-app/day request health, also folded from the Traefik access log: request
-- volume, 4xx/5xx counts and summed response time (additive, so it rolls up
-- incrementally). Averages/error rates are derived at query time.
CREATE TABLE IF NOT EXISTS analytics_perf (
    app_id     text   NOT NULL,
    day        date   NOT NULL,
    requests   int    NOT NULL DEFAULT 0,
    err_client int    NOT NULL DEFAULT 0,
    err_server int    NOT NULL DEFAULT 0,
    dur_ms_sum bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, day)
);

-- Latency histogram: one row per (app, day, bucket), counting requests whose
-- response time fell in that bucket (bucket edges in app/analytics.py). Counts
-- are additive, so p50/p95 can be estimated over any window without storing raw
-- per-request samples.
CREATE TABLE IF NOT EXISTS analytics_latency (
    app_id text     NOT NULL,
    day    date     NOT NULL,
    bucket smallint NOT NULL,
    count  int      NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, day, bucket)
);
"""


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(**PAAS_DB, min_size=1, max_size=5)

async def ensure_schema() -> None:
    async with _pool.acquire() as c:
        await c.execute(_SCHEMA)

async def close_pool() -> None:
    if _pool:
        await _pool.close()

def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("pool not initialised")
    return _pool
