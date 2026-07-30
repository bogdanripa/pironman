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
CREATE TABLE IF NOT EXISTS analytics_first_seen (
    app_id    text NOT NULL,
    visitor   text NOT NULL,
    first_day date NOT NULL,
    PRIMARY KEY (app_id, visitor)
);
CREATE TABLE IF NOT EXISTS analytics_state (k text PRIMARY KEY, v text);
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
