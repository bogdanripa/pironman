import json
import re

import asyncpg
from .config import PAAS_DB, SABLIER_EXCLUDE, STATIC_HOST_APP

# These ids are interpolated into the repair statements below, so they are
# constrained rather than trusted: they come from an env var, and an app id
# with a quote in it would be a SQL injection into the platform's own boot.
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")

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
-- uploaded for this app and is served by the shared static host. backend_routes
-- is vestigial: it held per-app path exceptions for an earlier resolution scheme
-- that needed them. The current rule (a bundle answers only reads, only for
-- files it has) makes exceptions unnecessary, so nothing reads the column; it is
-- left in place because dropping a column is destructive and buys nothing.
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS has_frontend boolean NOT NULL DEFAULT false;
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS backend_routes text[] NOT NULL DEFAULT '{}';

-- Redirect rules, ordered, as [{from, to, status}]. Applied by the static host
-- before anything else it does (see web/redirects.py). Stored as jsonb rather
-- than a table because they are always read and written as one ordered list —
-- order decides which rule wins.
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS redirects jsonb NOT NULL DEFAULT '[]'::jsonb;

-- Single-page app: a path the bundle does not contain is a client-side route,
-- so the entrypoint is served instead of a 404. Off by default — a typo should
-- be a 404, not a 200 showing the homepage — and only an app whose router
-- actually owns those paths should turn it on.
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS spa boolean NOT NULL DEFAULT false;

-- The path the container healthcheck requests. Recorded at create time and
-- applied when the container is first created, which happens on the app's first
-- CI deploy rather than at creation — the platform does not know an app's image
-- until its pipeline tells it.
ALTER TABLE IF EXISTS apps ADD COLUMN IF NOT EXISTS health_path text NOT NULL DEFAULT '/';

-- A frontend-only app has no image and no Coolify application, but the apps
-- table predates this codebase and declared both NOT NULL, so creating one
-- failed with a NotNullViolation. Relax them (idempotent — re-running on an
-- already-nullable column is a no-op).
ALTER TABLE IF EXISTS apps ALTER COLUMN image DROP NOT NULL;
ALTER TABLE IF EXISTS apps ALTER COLUMN coolify_uuid DROP NOT NULL;

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

-- When each app was last requested, to the second. The rollups above are keyed
-- by day, which cannot answer "is anyone still using this?" for an app that was
-- last touched an hour ago. Kept as its own tiny table rather than a column on
-- apps: the ingester derives it from hostnames in the access log and so knows
-- about traffic to apps this control plane never created.
CREATE TABLE IF NOT EXISTS analytics_last_seen (
    app_id    text PRIMARY KEY,
    last_seen timestamptz NOT NULL
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

-- Liveness for the background loops. A loop that dies takes the container with
-- it and is obvious; a loop that keeps running while achieving nothing is not,
-- and that is the failure this platform actually hits — an ingest cursor frozen
-- for a day, a sweep whose first sleep outlives every redeploy. Both looked
-- healthy from every angle: process up, container healthy, logs quiet.
--
-- Each task records when it last COMPLETED and how long it may reasonably go
-- without completing, so staleness is a fact any reader can evaluate without
-- knowing the schedules. Deliberately a plain table rather than an endpoint: the
-- reader that matters most runs OUTSIDE this process, because nothing inside
-- paas-api can report that paas-api is wedged.
CREATE TABLE IF NOT EXISTS task_heartbeat (
    task          text PRIMARY KEY,
    last_ok       timestamptz,
    last_error    text,
    last_error_at timestamptz,
    stale_after_s int NOT NULL
);

-- Every container this platform recycles, and why. Here for the same reason
-- task_heartbeat is: the reader that matters is outside the process. More
-- pointedly, a Coolify deploy builds a NEW container, so the control plane's
-- stdout is destroyed by precisely the event worth recording. Postgres survives
-- it. `reason` is prose aimed at whoever reads this weeks later; `detail` is for
-- anything structured. See app/events.py.
CREATE TABLE IF NOT EXISTS platform_events (
    id     bigserial PRIMARY KEY,
    at     timestamptz NOT NULL DEFAULT now(),
    app_id text NOT NULL,
    action text NOT NULL,
    reason text NOT NULL,
    detail jsonb
);
CREATE INDEX IF NOT EXISTS platform_events_at ON platform_events (at DESC);
CREATE INDEX IF NOT EXISTS platform_events_app ON platform_events (app_id, at DESC);
"""


# Best-effort data repairs, run after the schema and each independently. These
# are not structural: if one fails the platform still works, so a bad repair must
# never stop the control plane from booting. One did exactly that — an UPDATE
# assigning text (#>>) to a jsonb column has no implicit cast, so ensure_schema
# raised, the lifespan died and uvicorn exited 3 in a crash loop.
_REPAIRS = [
    # Rows written before the jsonb codec landed were double-encoded, stored as a
    # JSON *string* ("[]") rather than an array, and read back as text.
    """UPDATE apps SET redirects = (redirects #>> '{}')::jsonb
        WHERE jsonb_typeof(redirects) = 'string'""",

    # The static host inherited the default (sleep when idle) like any other app.
    # It fronts every app that sleeps and is what wakes them, so it sleeping would
    # strand all of them with nothing able to start anything. sablier.excluded()
    # refuses to enrol it regardless; this stops the row from claiming otherwise.
    f"""UPDATE apps SET sleep_when_idle = false, sablier_enrolled = false
        WHERE id = '{STATIC_HOST_APP}' AND sleep_when_idle""",

    # Everything else sablier.excluded() covers — the control plane — for the
    # same reason and a sharper one. `api` sat at sleep_when_idle = true all
    # along, harmless only while every caller asked excluded() first. One did
    # not: the _DEFRONTED repair read the column, believed it, and enrolled the
    # control plane in scale-to-zero, after which it slept five minutes after
    # every request and could not wake itself, because nothing fronts it.
    #
    # excluded() outranks the column everywhere now, so this is not what makes
    # the platform correct. It removes the bait — a row that says the one app
    # that must never sleep would like to sleep, waiting for the next reader who
    # forgets to ask.
    *[f"""UPDATE apps SET sleep_when_idle = false, sablier_enrolled = false
          WHERE id = '{a}' AND sleep_when_idle"""
      for a in sorted(SABLIER_EXCLUDE) if _SAFE_ID.fullmatch(a)],
]


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Decode jsonb to Python objects instead of raw strings.

    Without this asyncpg hands back the JSON text, so `list(row["redirects"])`
    iterates the *characters* of '[]' and yields ['[', ']'] — which then reaches
    the static host as a rule list of strings and breaks every request for that
    app. Registering the codec once fixes it everywhere rather than parsing at
    each call site.
    """
    await conn.set_type_codec(
        "jsonb", schema="pg_catalog",
        encoder=json.dumps, decoder=json.loads, format="text")


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(**PAAS_DB, min_size=1, max_size=5,
                                      init=_init_conn)

async def ensure_schema() -> None:
    """Structural schema first — that must succeed, since nothing works without
    it. Then each repair on its own, swallowed: a data fix that fails leaves the
    platform working, whereas letting it raise here takes the control plane down
    and prevents the deploy that would fix it."""
    async with _pool.acquire() as c:
        await c.execute(_SCHEMA)
        for repair in _REPAIRS:
            try:
                await c.execute(repair)
            except Exception:
                pass

async def close_pool() -> None:
    if _pool:
        await _pool.close()

def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("pool not initialised")
    return _pool
