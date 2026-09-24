import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from fastapi import FastAPI

from .db import init_pool, ensure_schema, close_pool, pool
from . import (heartbeat, autoupdate, analytics, alerts, events, routing,
               sablier, deploys)
from .cors import CorsMiddleware
from .config import DASHBOARD_ORIGIN, app_url
from .routers import (apps, crons, query, scaffold, env, refresh, ghsecrets,
                      redirects as redirects_router,
                      domains as domains_router,
                      analytics as analytics_router, stats,
                      alerts as alerts_router, frontend, host)


log = logging.getLogger("pironman")


def _configure_logging() -> None:
    """Give the root logger a handler and a level. Nothing here did before.

    uvicorn's default config touches only the `uvicorn*` loggers, so the root
    logger kept zero handlers and its default WARNING level. WARNING and above
    still surfaced, through Python's `lastResort` handler — which is why
    `_swallow`'s log.exception and the analytics warnings have always been
    visible — but **every INFO line was silently discarded**. So
    `analytics: counted N lines` has never once been printed, in a loop that has
    run every two minutes for months, and "the log is quiet" could not
    distinguish a healthy pass from a truncating one. Success signals that do
    not exist are not a cosmetic problem: they are the difference between a
    failure you can see and one you infer.

    PAAS_LOG_LEVEL overrides it, so a noisy day can be turned down to WARNING
    without a deploy-time code change.
    """
    root = logging.getLogger()
    if root.handlers:      # something already configured it — do not double up
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(handler)
    root.setLevel(os.getenv("PAAS_LOG_LEVEL", "INFO").upper())


_configure_logging()


def _swallow(what: str) -> None:
    """Report a background failure and carry on.

    The loops must not die — a bad analytics pass or a failed sweep cannot be
    allowed to take the control plane with it — but swallowing the exception
    *silently* is how a broken ingester ran for hours looking healthy. uvicorn
    captures this, so `apps_logs api` shows it.
    """
    log.exception("background task failed: %s", what)


class RedactKeyFilter(logging.Filter):
    """Keep the connector's `?key=` out of the access log.

    claude.ai connectors cannot send an Authorization header, so the key rides in
    the query string (see PromoteKeyMiddleware) — and uvicorn logs the full path.
    That put a working **admin** key in plaintext in this container's logs, where
    `apps_logs api` hands it to anyone who asks, including the model itself.
    Redacting at the log record is the fix that cannot be forgotten: it covers
    every path, not just the ones someone remembered to sanitise.
    """

    _KEY = re.compile(r"([?&]key=)[^&\s\"\']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._KEY.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self._KEY.sub(r"\1<redacted>", record.msg)
        return True


class DropHealthchecksFilter(logging.Filter):
    """Keep the container healthcheck out of the access log.

    It runs every 10 seconds, so it writes ~8,600 lines a day and pushes
    everything else out of `docker logs --tail`. A background traceback would
    scroll out of reach within about two minutes, which defeats the point of
    logging it at all. Only *successful* checks are dropped — a failing one is
    worth seeing, and shows up as an unhealthy container besides.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        return not (isinstance(a, tuple) and len(a) >= 5
                    and a[1] == "GET" and a[2] == "/health" and a[4] == 200)


for _f in (RedactKeyFilter(), DropHealthchecksFilter()):
    logging.getLogger("uvicorn.access").addFilter(_f)


class PromoteKeyMiddleware:
    """Promote a ?key= query param on /mcp into an Authorization header.

    claude.ai custom connectors cannot send an Authorization header, so the key
    rides in the connector URL as ?key=… and we turn it into a bearer header the
    endpoints' auth dependency understands.

    This is a **pure ASGI** middleware on purpose. The obvious way to write it,
    `@app.middleware("http")`, is Starlette's BaseHTTPMiddleware, which wraps and
    buffers the response body. That mangles the streaming SSE responses the MCP
    transport at /mcp emits, and the Anthropic connector proxy then rejects them
    as "Invalid content from server" (curl tolerates the mangling; the strict
    proxy does not). A pure ASGI middleware only rewrites the request scope and
    passes `send` through untouched, so the SSE stream is delivered verbatim.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            headers = scope.get("headers") or []
            if not any(k == b"authorization" for k, _ in headers):
                qs = parse_qs(scope.get("query_string", b"").decode())
                key = (qs.get("key") or [None])[0]
                if key:
                    scope = dict(scope)
                    scope["headers"] = list(headers) + [
                        (b"authorization", f"Bearer {key}".encode())
                    ]
        await self.app(scope, receive, send)

SERVER_DESCRIPTION = """\
Deploy and manage web applications on a self-hosted Raspberry Pi 5.

An **app** is one Docker container plus, optionally, one database and any number
of scheduled jobs. Creating an app gives it a public HTTPS URL automatically at
https://<id>-coolify.bogdanripa.com — there is no DNS, certificate or proxy step
to perform.

What you can do here: list what is deployed, create a new app (a static frontend,
a backend docker image, or both), publish or update an app's frontend, adopt an
app that already exists in Coolify but was made by hand, change
an app's image, attach or drop an app's database, read an app's container logs
and status, see live resource use (whether each app is running, its CPU/RAM
right now, its database size and recent request health) via apps_stats, delete an
app, set
shared and per-app environment variables, run SQL or mongosh scripts against an
app's own database, manage scheduled HTTP calls to an app, create, list or
delete a GitHub repository's Actions secrets, and read traffic analytics —
unique visitors, DAU/WAU/MAU, daily trends and weekly retention cohorts — for
one app or across the whole platform.

For anything the tools above do not cover, host_run_script runs a shell script
on the Raspberry Pi itself — the host, as root, not inside any app's container —
so `df -h`, `docker ps`, `/etc`, `systemctl` and the crontab are all reachable.
Reach for it for host-level work (disk pressure, OS config, a log no tool
exposes) and not as a shortcut around a purpose-built tool: apps_logs,
apps_stats and db_run_script give better answers for the things they cover.

Analytics are automatic: every app is measured at the shared edge proxy, so a
new app starts producing numbers as soon as it serves traffic, with nothing to
add to the app itself. A visitor is a cookieless salted hash of IP + user-agent,
counted as one person across apps. Use analytics_overview for headline numbers
(unique visitors, DAU/WAU/MAU, a humans-vs-bots split, and a per-app breakdown
when no app is given), analytics_timeseries for a daily line chart,
analytics_cohorts for retention, analytics_agents for the top user-agents, and
analytics_recent for a live tail of the most recent HTTP requests, each with the
caller's real IP — the one place a raw address is reported. apps_stats
gives the live infra view (running state, CPU/RAM, disk, DB size, and request
error rate / latency percentiles). A human-facing dashboard of all of this runs
as its own frontend-only app at https://dashboard-coolify.bogdanripa.com — it
asks for an API key on first load and keeps it in the browser.

If a Telegram bot is configured on the box, the platform also messages you when
an app goes down, recovers, or starts throwing 5xx errors; alerts_test confirms
that wiring.

Apps scale to zero when idle (Sablier): they stop after a few idle minutes and
start again on the next request. apps_update turns this on or off per app; the
control plane itself never sleeps.

**You never tell the platform what an app is made of.** Creating an app
registers an id and hands it a hostname; nothing runs yet. What it becomes is
decided by its pipeline: it is a frontend once CI uploads a bundle, a backend
once CI deploys an image (which creates its container), or both. Information
flows from GitHub to the platform, never the other way, so there is no image and
no repository to name at creation.

**Choose the app's shape first.** An app has up to three parts — a static
frontend, a backend container, and a database — and picking the right combination
matters more than any other decision here:

- **Frontend only**: a static site, an SPA, a browser game, a landing page. No
  container at all, so nothing to keep running, no cold start, and the assets are
  CDN-cacheable. Its CI uploads built files.
- **Backend only**: an API, an MCP server, a worker — anything with no browser UI
  of its own. Its CI builds and deploys an image.
- **Both**: the usual web app — its CI does both. They share one
  hostname, so the frontend calls its API with a relative path — no CORS, no API
  base URL to configure.

Default to **splitting** a UI-plus-API app into both rather than baking the UI
into the backend container: the assets then come from the shared static host and
the CDN instead of the container. So "a game with a leaderboard", "a to-do app",
"a dashboard with an API" are all frontend + backend + database.

With both, requests resolve with one rule and nothing to configure: a file in the
bundle is served (with "/" resolving to index.html), and everything else — every
write, and every path the bundle does not contain — goes to the backend. So an
API under /api works, but so does any other path: OAuth callbacks, downloads and
server-rendered pages are simply paths the bundle does not have. A path nobody
has at all is a 404 — the bundle's own 404.html if it ships one. A
single-page app whose router owns such paths sets apps_update spa=true and gets
index.html instead, so its deep links work.

Any app can also carry **redirect rules** (apps_redirects_list / apps_redirects_set)
— ordered, first match wins, applied ahead of files and the backend. They support
'*' (available in the target as ':splat') and ':name' segment placeholders, take
301/302/307/308, preserve the query string, and can point at another path or an
absolute URL. Setting them needs no redeploy, and works for a backend-only app
too. Reach for these when paths move, rather than adding redirect code to an app.

An app can also answer on a **custom domain** of its own (apps_domains_list /
apps_domain_add / apps_domain_remove) — 'shop.example.com' alongside
https://shop-coolify.bogdanripa.com, which it keeps. Everything follows the
hostname: the bundle, the backend, the wake-on-request for a sleeping app and
the analytics attribution all work on it exactly as on the generated one. Two
things are NOT done for you, and both fail in ways that look like a platform
fault. Create the DNS record first: an **A record to the box's public IP**,
proxied — for a subdomain as much as for an apex. A CNAME to
<something>-coolify.bogdanripa.com looks right and is not: those names are
themselves proxied by Cloudflare, so the request never leaves Cloudflare and is
answered there with a 404 that leaves no trace on the box. And if the domain is
proxied, set its SSL mode to 'Flexible': this origin serves plain HTTP and has no
certificate, so a 'Full' domain fails on https:// while http:// works. Keep using the generated hostname for anything internal (CI, crons, one
app calling another): it cannot be removed, and a custom domain can.

Frontends deploy by upload, not by image: apps_frontend_write publishes a small
site from inline files (no build, no repo — good for a landing page), while a
real build ships from CI, which zips the build output and uploads it with the
app's deploy key. apps_deploy_workflow returns both halves — the backend job and
an optional frontend job — so an app can use either or both.

Three rules govern almost every mistake:

1. **Images must be built for linux/arm64.** The host is a Raspberry Pi. An
   amd64 image will pull successfully and then fail to start with an exec format
   error. In a GitHub Actions build this means `platforms: linux/arm64`.
2. **The container must listen on port 80 on both IP families.** Not 3000/8080
   (→ 502 from the proxy). Two clients connect from different directions: the
   in-container healthcheck hits `localhost` (IPv6 `::1` first), and the proxy
   connects to the container's IPv4 address. An IPv4-only bind (`0.0.0.0`) fails
   the healthcheck and the deploy is rolled back; an IPv6-only bind passes the
   healthcheck and then 502s every real request — the app reports healthy while
   serving nothing. Node's `listen(80, '::')` is dual-stack; **Python's is not**
   (`uvicorn --host ::` is IPv6-only — bind a `dualstack_ipv6=True` socket and
   pass its fd). Port 80 is privileged, so run as root (no `USER` line).
3. **App ids become hostnames.** Lowercase, alphanumeric and hyphens only, one
   flat label, fixed once created.

Databases are per-app and provisioned on request. The connection string is
injected into the container as the DATABASE_URL environment variable on every
deploy and recomposed each time, so an app should read it from the environment
rather than hardcoding anything. A database can be attached to an app after
creation (apps_attach_db) or dropped from it (apps_detach_db — this destroys the
data), not only at create time.

**Internal services** are reachable from apps on this box and from nowhere else,
the same way a database is: they are created with no domain, so Traefik generates
no router and there is no public hostname to reach. Their address arrives in the
environment on every deploy, exactly like DATABASE_URL — read it from there
rather than hardcoding it, so the app keeps working if the service is ever
rebuilt.

`RAG_URL` — document retrieval. POST a document, get a 16-character id back,
then ask questions scoped to the ids you hold. It accepts PDF, Word, PowerPoint,
HTML, Markdown, plain text, or a public URL to fetch. Ingest is asynchronous:

    POST $RAG_URL/documents        {"text": "..."}   or {"base64": "..."}
                                   or {"url": "https://..."}, optional "filename"
      -> 202 {"id": "Ab3xK...", "status": "pending"}
    POST $RAG_URL/documents/status {"ids": ["Ab3xK..."]}
      -> status pending | ready | failed, with "error" explaining a failure and
         "truncated" when only part of the document was indexed
    POST $RAG_URL/query            {"ids": ["Ab3xK..."], "q": "...", "k": 8}
      -> {"results": [{"id", "text", "page", "heading", "score"}], "truncated": [...]}
    POST $RAG_URL/documents/delete {"ids": ["Ab3xK..."]}

Four things to build around rather than discover:

- **The id is the credential.** There is no API key; anyone holding an id can
  read that document. Store ids like passwords, and never put one in a URL, a
  log line or a redirect — which is why every endpoint above takes them in the
  POST body and none has a path parameter.
- **Poll for `ready` before querying.** A document that has not finished
  indexing returns no results rather than an error, so a query fired
  immediately after upload will look like an empty document.
- **Honour `truncated` in the query response.** Large documents are indexed only
  up to a page/token limit. If a result set says truncated, the answer may be
  missing from the part that was never indexed — say so rather than answering
  confidently from half a contract.
- **`failed` is normal and worth surfacing.** A scanned PDF with no text layer,
  a Google Docs link that returns a sign-in page, or a URL pointing at a private
  address all fail ingest with a reason. Show the reason; do not retry blindly.

Apps also take environment variables in two scopes. **Shared** variables are set
once and injected into every app — the place for account-wide secrets such as an
OpenAI API key, since this is a single owner's box. **App-specific** variables
apply to one app and override a shared variable of the same name. Setting or
removing a variable redeploys the affected app(s) so it reaches the running
container; a shared change therefore redeploys everything. Values are write-only:
you can set and overwrite them but never read one back — listings show a masked
preview only, so re-set a variable if you are unsure of its value.

Deploys go through CI, not through hand-built images. The right way to build and
deploy an app is to wire its repository to GitHub Actions: each push to main
builds the arm64 image, pushes it to ghcr.io tagged :latest, and calls the app's
/refresh hook. The box watches that tag and redeploys the new image — on the
/refresh call, and hourly regardless — so there is deliberately no tool here to
deploy an app by hand.

CI authenticates with one repository secret, PAAS_KEY: the app's own scoped
deploy key, used by both the backend /refresh call and the frontend upload. Do
not ask the user to create it — apps_create returns the key and github_secret_set
installs it, so wiring an app up needs no human step. The key can only deploy
that one app, and the box never accepts an image from the caller: /refresh just
makes it re-check the tag it already watches. Do NOT build an
image locally and push it to the registry yourself, and do not go looking for
registry or deploy credentials to do so — that is the wrong path, it will not
match the arm64 platform this platform expects, and it does not reproduce. To
set this up, call apps_deploy_workflow and write exactly what it returns into the
app's repo; to ship a change after that, commit and push. Building an image by
hand is only ever a first-time bootstrap step for the control plane itself.

Creating the app itself is the one-off you do here, before its first CI run.
apps_create and apps_deploy_workflow each return an ordered deploy runbook —
follow it to wire up CI and ship the app end to end yourself, with no repository
secret and no human step. New apps auto-update by default; apps_update turns
it off (e.g. to hold a manual rollback).

The whole platform runs on one small machine at home. Deleting an app destroys
its database and all its data with no undo, running a database script is an
unguarded pipe into that database, and host_run_script is an unguarded root
shell on the machine all of it runs on — confirm all three with the user first.\
"""


async def _sync_routes(reason: str) -> None:
    """Re-derive the static host's routers from the database.

    Necessary because this Coolify build rejects the readonly-labels flag, so a
    redeploy of the static host can regenerate its label block and drop the
    per-app routers we wrote — after which a frontend app's hostname resolves
    nowhere until something republishes it. The label set is a pure function of
    the apps table and the write is skipped when nothing changed, so running this
    routinely costs one label comparison and never restarts anything needlessly.
    """
    try:
        async with pool().acquire() as c:
            await routing.sync_frontend_routes(c)
    except Exception:
        _swallow(f"route sync ({reason})")  # drift must never take the box down


SWEEP_EVERY = 3600
# Floor on the catch-up: however overdue a sweep is, wait at least this long
# after boot. A sweep pulls every watched image, and "overdue" is exactly what a
# crash-looping control plane looks like — without a floor, a restart loop would
# turn into a registry-pull loop. Two minutes is longer than a crash loop
# survives and shorter than anything the sweep is meant to catch.
SWEEP_MIN_DELAY = 120


async def _until_next_sweep() -> float:
    """Seconds to wait before the next sweep, measured from the last one that
    actually COMPLETED rather than from this process starting.

    The difference is the whole point. A flat `sleep(3600)` measures the
    container's lifetime, and on a control plane that redeploys itself those are
    not the same number: every restart put the first sweep an hour away again, so
    six deploys in ninety minutes meant no sweep at all — 1h47m of nothing, while
    every other loop stayed current and the container reported healthy. The
    heartbeat caught it, which is what it is for, but the loop should not need
    catching.
    """
    since = await heartbeat.since_last_ok("autoupdate_sweep")
    if since is None:
        return SWEEP_EVERY  # never swept, or the lookup failed: full interval
    return max(SWEEP_MIN_DELAY, SWEEP_EVERY - since)


async def _autoupdate_loop():
    """Hourly: redeploy every opted-in app whose watched image tag has moved.

    The interval is counted from the last sweep that completed, not from this
    process starting — see `_until_next_sweep`. So a restart no longer costs a
    full hour, and a control plane that redeploys often still sweeps roughly
    hourly instead of never.

    The heartbeat stays, and matters as much as it did: it is what caught this
    loop starving in the first place, and it is still the only thing that can
    report a sweep that runs but achieves nothing."""
    # The Sablier repair is the exception to that delay and runs BEFORE the first
    # sleep. What makes the sweep expensive is pulling every watched image; this
    # is one `docker ps -a` per sleeping app, so there is nothing to save by
    # postponing it. What it repairs is an app whose container is gone, which
    # cannot wake and serves 502s for every second it waits — and on a box that
    # redeploys this control plane more often than hourly, the timer below resets
    # before the sweep ever fires, so a repair left behind it would never run at
    # all. Startup is also when it is most likely to be needed.
    try:
        await sablier.reconcile()
    except Exception:
        _swallow("sablier reconcile (startup)")
    while True:
        await asyncio.sleep(await _until_next_sweep())
        try:
            await autoupdate.check_all()
        except Exception as e:
            _swallow("auto-update sweep")
            await heartbeat.beat("autoupdate_sweep", error=repr(e))
        # Same cadence: repair anything a Coolify label regeneration undid — the
        # static host's per-app routers, and each app's Sablier enrollment. Both
        # fail silently (a hostname resolves nowhere; an app quietly stops
        # sleeping), so nothing but a sweep would find them.
        await _sync_routes("hourly sweep")
        try:
            await sablier.reconcile()
        except Exception as e:
            _swallow("sablier reconcile")
            await heartbeat.beat("autoupdate_sweep", error=repr(e))
        else:
            # Beat once the whole sweep is done — the redeploy check, the route
            # sync and the Sablier repair are one unit of work, and a partial
            # sweep is not a working one.
            await heartbeat.beat("autoupdate_sweep")


async def _analytics_loop():
    """Fold new Traefik access-log lines into the analytics rollups on a short
    timer. Idempotent (StartUTC cursor), so a missed or overlapping tick is
    harmless; a failure is swallowed so it can never take the control plane down."""
    while True:
        try:
            await analytics.ingest_once()
        except Exception as e:
            _swallow("analytics ingest")
            await heartbeat.beat("analytics_ingest", error=repr(e))
        else:
            await heartbeat.beat("analytics_ingest")
        await asyncio.sleep(120)


async def _alerts_loop():
    """Check every app for down/recovered/5xx transitions and notify Telegram.
    No-op when Telegram is not configured. Runs after ingestion cadence so the
    5xx counts it reads are fresh; failures are swallowed."""
    while True:
        await asyncio.sleep(150)
        try:
            await alerts.check_once()
        except Exception as e:
            _swallow("alert check")
            await heartbeat.beat("alerts_check", error=repr(e))
        else:
            await heartbeat.beat("alerts_check")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_pool()
    await ensure_schema()  # create env tables if missing — no manual SQL on the Pi
    # Declare the background tasks before starting them, so one that never runs
    # is visible as a stale row rather than as no row at all — an absent task is
    # invisible to every reader, which is how a dead cron dispatcher went
    # unnoticed while the watchdog reported everything current.
    await heartbeat.register()
    await events.trim()   # bound the recycle log; best-effort, never fatal
    await deploys.trim()  # and the CI deploy log, same contract
    await _sync_routes("startup")
    tasks = [asyncio.create_task(_autoupdate_loop()),
             asyncio.create_task(_analytics_loop()),
             asyncio.create_task(_alerts_loop())]
    yield
    for t in tasks:
        t.cancel()
    await close_pool()


app = FastAPI(
    title="paas-api",
    description=SERVER_DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
)


# Pure-ASGI, so it never buffers the /mcp SSE stream (see PromoteKeyMiddleware).
# Use a dedicated key for the connector URL so it can be revoked on its own — it
# ends up in the connector config and in edge logs.
app.add_middleware(PromoteKeyMiddleware)

# The dashboard is its own app on its own hostname, so its fetch calls are
# cross-origin. Narrow on purpose: read-only analytics/stats paths only, and it
# never touches /mcp (see app/cors.py).
app.add_middleware(CorsMiddleware, allow_origins=(DASHBOARD_ORIGIN,))


# Router order controls how tools list at /mcp. operation_ids are prefixed by
# group (apps_*, crons_*, db_*, host_*, env_*) so the flat MCP tool list reads as
# coherent groups: apps (lifecycle + deploy + per-app env), schedules,
# database, the host itself, and shared env.
app.include_router(apps.router)
app.include_router(scaffold.router)   # apps_deploy_workflow — part of the apps group
app.include_router(env.app_router)    # apps_env_* — per-app env, part of the apps group
app.include_router(crons.router)
app.include_router(query.router)
app.include_router(host.router)        # host_run_script — a shell on the box itself
app.include_router(env.shared_router)  # env_* — shared, account-wide variables
app.include_router(refresh.router)     # POST /apps/{id}/refresh — CI deploy hook
app.include_router(ghsecrets.router)   # github_secret_* — repo Actions secrets
app.include_router(analytics_router.router)  # analytics_* — cross-app visitor stats
app.include_router(stats.router)       # apps_stats — live CPU/RAM/DB/health snapshot
app.include_router(alerts_router.router)  # alerts_test — Telegram alert wiring check
app.include_router(frontend.router)    # apps_frontend_deploy / apps_frontend_write
app.include_router(redirects_router.router)  # apps_redirects_list / _set
app.include_router(domains_router.router)    # apps_domains_list / apps_domain_add / _remove


@app.get("/analytics/dashboard", include_in_schema=False)
async def dashboard_moved():
    """The dashboard is its own app now (a frontend on the platform, rather than
    HTML embedded in this control plane). Keep the old URL working."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(app_url("dashboard"), status_code=308)


@app.exception_handler(Exception)
async def _log_unhandled(request, exc):
    """Log the traceback of anything that reaches the top of a request.

    Nothing did. An unhandled exception in a route normally reaches uvicorn's
    protocol layer, which prints "Exception in ASGI application" with the
    traceback — but **fastapi-mcp does not go through uvicorn**. It invokes the
    route in-process over an ASGI transport, so a tool call that raises produced
    a bare 500 with no traceback, no access-log line, and nothing in the
    container log but the MCP layer's own one-liner:
    `ERROR:fastapi_mcp.server:Error calling <tool>. Status code: 500.`

    That is exactly what apps_detach_db did on 2026-09-24. The bug was findable
    only by reading the code and reproducing the Coolify call by hand; the box
    itself had no record of which line raised. A failure whose only trace says
    "something went wrong" is the same defect this platform is built against,
    one level up — so the handler exists to make the trace real, and the body
    now names where to look instead of saying "Internal Server Error".
    """
    log.exception("unhandled error in %s %s", request.method, request.url.path)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={"detail": f"internal error handling {request.method} "
                           f"{request.url.path} — the traceback is in the api "
                           f"container's log (apps_logs api)"})


@app.get("/health", tags=["meta"], operation_id="health", include_in_schema=False)
async def health():
    return {"ok": True}


# MCP: the same endpoints exposed as tools at /mcp. Mounted in-process so it
# shares this app's auth and lifespan. Tool names come from operation_id,
# descriptions from each endpoint's summary and docstring.
from fastapi_mcp import FastApiMCP  # noqa: E402

# fastapi-mcp 0.4.0 exposes no config for the MCP HTTP transport, and its
# defaults do not work with the claude.ai connector proxy. Patch the SDK session
# manager it builds under the hood:
#   - stateless=True — the connector caches a session id, but this control plane
#     redeploys ITSELF constantly and MCP sessions live in memory, so every
#     redeploy kills them and every stateful call afterwards fails with "Invalid
#     content from server". Stateless makes each tool call self-contained, so it
#     survives redeploys and needs no session bookkeeping.
#   - json_response=False — answer tool calls as SSE (text/event-stream), which
#     the connector proxy expects rather than a single application/json body.
# Wrapped in try/except so a fastapi-mcp/SDK rename can never crash startup (the
# control plane must always boot); at worst the connector stays broken.
try:
    import fastapi_mcp.transport.http as _fmhttp  # noqa: E402

    _BaseSessionManager = _fmhttp.StreamableHTTPSessionManager

    class _PatchedSessionManager(_BaseSessionManager):
        def __init__(self, *args, **kwargs):
            kwargs["stateless"] = True
            kwargs["json_response"] = False
            kwargs["event_store"] = None  # unused (and disallowed) when stateless
            super().__init__(*args, **kwargs)

    _fmhttp.StreamableHTTPSessionManager = _PatchedSessionManager
except Exception:  # pragma: no cover - never block startup on a patch failure
    pass

# apps_update_code (PUT /apps/{id}/code) stays a REST route — it is what the CI
# workflow curls to redeploy — but it is NOT exposed as an MCP tool. Deploys go
# through CI; giving the model a manual-deploy tool only invites it to skip the
# pipeline. The route keeps serving; it just isn't in the tool list.
_mcp = FastApiMCP(
    app,
    name="pironman",
    description=SERVER_DESCRIPTION,
    # apps_frontend_deploy takes a raw zip body — a CI call, not something a model
    # can meaningfully construct, so it stays REST-only like apps_update_code.
    exclude_operations=["apps_update_code", "apps_refresh", "apps_refresh_status",
                        "apps_frontend_deploy",
                        # plumbing reached through apps_update, which expresses
                        # intent ("give this app a backend") rather than mechanism
                        # ("set its image"). Deploys are CI's job, not a tool call.
                        "apps_set_image", "apps_autoupdate", "apps_sablier"],
)

# fastapi-mcp does `Server(name, description)`, and the low-level MCP Server takes
# `version` as its second positional arg — so our whole description lands in
# serverInfo.version as a 1500-char markdown blob. That is what the connector
# proxy chokes on at the handshake. Put a real version back, and move the text to
# `instructions`, which is where server-level guidance belongs (and where the
# model actually reads it — in the version field it was simply wasted).
if getattr(_mcp, "server", None) is not None:
    _mcp.server.version = "0.1.0"
    _mcp.server.instructions = SERVER_DESCRIPTION

# Tag each tool read-only / write / destructive. Two things read these, and the
# second is the one that bites: the claude.ai connector UI groups tools by them,
# and the connector's approval gate keys on them. A `destructiveHint=True` tool
# prompts before every call — including inside an unattended Routine, where there
# is nobody to answer. This set is therefore not cosmetic; it decides which tools
# a scheduled run can reach at all. Guarded so a types rename can't block startup.
#
# Verified 2026-08-03: the nightly audit fired at ~02:00 and sat blocked on its
# very first call until 07:35, because its liveness probe is host_run_script and
# that was tagged destructive. It resumed only because a human happened to
# approve it. That routine's contract is "silence means healthy", so a run that
# never starts looks exactly like a clean one — the failure this platform is
# built against. host_run_script and db_run_script are deliberately NOT in
# _DESTRUCTIVE for that reason; README "Running a routine unattended" records
# what stands in place of the gate they used to carry.
#
# _READONLY must hold every GET-backed tool. Anything missing falls to the `else`
# branch and is advertised as state-changing — which was true of
# platform_tasks_health and platform_events, both plain GETs, the first being the
# highest-signal check the audit makes. Derive this list from the route methods
# (`grep operation_id app/routers/`), never from what a tool's name suggests.
try:
    from mcp.types import ToolAnnotations  # noqa: E402

    # Derived from the ROUTE METHOD, not from what a tool's name suggests —
    # every hand-maintained list here has drifted at least once. GET is
    # read-only and idempotent; DELETE is destructive and idempotent (deleting
    # twice lands in the same place); PUT is idempotent; POST is neither.
    _METHOD_HINTS = {
        "GET":    dict(readOnlyHint=True,  destructiveHint=False, idempotentHint=True),
        "DELETE": dict(readOnlyHint=False, destructiveHint=True,  idempotentHint=True),
        "PUT":    dict(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
        "PATCH":  dict(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
        "POST":   dict(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    }

    # Overrides, each for a reason the method cannot express.
    #
    # The two script tools are the ones that cost something. They are POSTs, so
    # the method says "not destructive", and this repo previously left them that
    # way on purpose: the annotation is static per tool, so marking them
    # destructive prompts identically for `docker ps` and for `rm -rf`, and
    # nearly every call is a read. Verified 2026-08-03, the nightly audit fired
    # at ~02:00 and sat blocked until 07:35 because its liveness probe is
    # host_run_script and the connector's approval gate keys on this flag.
    #
    # They are marked destructive now anyway, at the owner's instruction: the
    # Tasks agent platform hides destructiveHint tools unless an admin allows
    # them, and an unreviewed root shell is the wrong thing to leave in the
    # hands of an autonomous agent by default. The cost is real and is NOT
    # solved in this file — any claude.ai Routine that calls either tool must be
    # granted an explicit allowance, or it will block on its first call exactly
    # as the 2026-08-03 audit did. README "Running a routine unattended" carries
    # the warning.
    _DESTRUCTIVE = {"host_run_script", "db_run_script"}

    # The mirror image: a POST that genuinely only reads. db_read_query sends a
    # query in a body, which is why it is a POST rather than a GET, but it
    # connects as a role holding SELECT and nothing else inside a read-only
    # transaction — so the method says "writes" and the database says otherwise.
    # The database is right. Kept as a short explicit list because the honest
    # alternative, trusting the verb, would hide this tool from exactly the
    # autonomous callers it was built for.
    _READ_ONLY = {"db_read_query"}

    # Reaches something outside this box. Everything else acts on the Pi alone,
    # which is a closed world: the tool's effects are bounded by the machine.
    _OPEN_WORLD = {"github_secret_set", "github_secret_delete",
                   "github_secrets_list", "apps_create", "apps_deploy_key"}

    # operation_id -> HTTP method, read off the routes themselves.
    _methods: dict[str, str] = {}
    for _route in app.routes:
        _op = getattr(_route, "operation_id", None)
        for _m in (getattr(_route, "methods", None) or ()):
            if _op and _m not in ("HEAD", "OPTIONS"):
                _methods[_op] = _m

    _unannotated = []
    for _tool in getattr(_mcp, "tools", None) or []:
        _method = _methods.get(_tool.name)
        if _method is None:
            _unannotated.append(_tool.name)
        hints = dict(_METHOD_HINTS.get(_method or "POST", _METHOD_HINTS["POST"]))
        if _tool.name in _DESTRUCTIVE:
            hints["readOnlyHint"] = False
            hints["destructiveHint"] = True
        elif _tool.name in _READ_ONLY:
            hints["readOnlyHint"] = True
            hints["destructiveHint"] = False
            hints["idempotentHint"] = True
        hints["openWorldHint"] = _tool.name in _OPEN_WORLD
        _tool.annotations = ToolAnnotations(**hints)

    # A tool whose method could not be resolved was annotated from the POST
    # fallback — safe (not read-only, not destructive), but it means the
    # derivation missed something, and a read-only tool advertised as
    # state-changing is how platform_tasks_health and platform_events were
    # mis-tagged for months. Say so rather than letting it pass silently.
    if _unannotated:
        log.warning("MCP tools with no resolvable route method, annotated as "
                    "POST: %s", ", ".join(sorted(_unannotated)))
except Exception:  # pragma: no cover - never block startup on annotations
    log.warning("could not set MCP tool annotations", exc_info=True)

_mcp.mount_http()
