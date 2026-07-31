import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from fastapi import FastAPI

from .db import init_pool, ensure_schema, close_pool, pool
from . import autoupdate, analytics, alerts, routing
from .cors import CorsMiddleware
from .config import DASHBOARD_ORIGIN, app_url
from .routers import (apps, crons, query, scaffold, env, refresh, ghsecrets,
                      redirects as redirects_router,
                      analytics as analytics_router, stats,
                      alerts as alerts_router, frontend)


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

Analytics are automatic: every app is measured at the shared edge proxy, so a
new app starts producing numbers as soon as it serves traffic, with nothing to
add to the app itself. A visitor is a cookieless salted hash of IP + user-agent,
counted as one person across apps. Use analytics_overview for headline numbers
(unique visitors, DAU/WAU/MAU, a humans-vs-bots split, and a per-app breakdown
when no app is given), analytics_timeseries for a daily line chart,
analytics_cohorts for retention, analytics_agents for the top user-agents, and
analytics_recent for a live tail of the most recent HTTP requests. apps_stats
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
server-rendered pages are simply paths the bundle does not have. A client-side
route the backend also rejects falls back to index.html, so SPA deep links work
too.

Any app can also carry **redirect rules** (apps_redirects_list / apps_redirects_set)
— ordered, first match wins, applied ahead of files and the backend. They support
'*' (available in the target as ':splat') and ':name' segment placeholders, take
301/302/307/308, preserve the query string, and can point at another path or an
absolute URL. Setting them needs no redeploy, and works for a backend-only app
too. Reach for these when paths move, rather than adding redirect code to an app.

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
its database and all its data with no undo, and running a database script is an
unguarded pipe into that database — confirm both with the user first.\
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
        pass  # routing drift must never take the control plane down


async def _autoupdate_loop():
    """Hourly: redeploy every opted-in app whose watched image tag has moved.
    First run is an hour after startup, not on boot — this control plane
    redeploys itself often, and there is no need to sweep on every restart."""
    while True:
        await asyncio.sleep(3600)
        try:
            await autoupdate.check_all()
        except Exception:
            pass  # a bad sweep must never take the control plane down
        # Same cadence: repair any routing the static host lost to a label
        # regeneration, so a frontend never stays unreachable for longer than the
        # sweep interval.
        await _sync_routes("hourly sweep")


async def _analytics_loop():
    """Fold new Traefik access-log lines into the analytics rollups on a short
    timer. Idempotent (StartUTC cursor), so a missed or overlapping tick is
    harmless; a failure is swallowed so it can never take the control plane down."""
    while True:
        try:
            await analytics.ingest_once()
        except Exception:
            pass
        await asyncio.sleep(120)


async def _alerts_loop():
    """Check every app for down/recovered/5xx transitions and notify Telegram.
    No-op when Telegram is not configured. Runs after ingestion cadence so the
    5xx counts it reads are fresh; failures are swallowed."""
    while True:
        await asyncio.sleep(150)
        try:
            await alerts.check_once()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_pool()
    await ensure_schema()  # create env tables if missing — no manual SQL on the Pi
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
# group (apps_*, crons_*, db_*, env_*) so the flat MCP tool list reads as
# coherent groups: apps (lifecycle + deploy + per-app env), schedules,
# database, and shared env.
app.include_router(apps.router)
app.include_router(scaffold.router)   # apps_deploy_workflow — part of the apps group
app.include_router(env.app_router)    # apps_env_* — per-app env, part of the apps group
app.include_router(crons.router)
app.include_router(query.router)
app.include_router(env.shared_router)  # env_* — shared, account-wide variables
app.include_router(refresh.router)     # POST /apps/{id}/refresh — unauth CI hook
app.include_router(ghsecrets.router)   # github_secret_* — repo Actions secrets
app.include_router(analytics_router.router)  # analytics_* — cross-app visitor stats
app.include_router(stats.router)       # apps_stats — live CPU/RAM/DB/health snapshot
app.include_router(alerts_router.router)  # alerts_test — Telegram alert wiring check
app.include_router(frontend.router)    # apps_frontend_deploy / apps_frontend_write
app.include_router(redirects_router.router)  # apps_redirects_list / _set


@app.get("/analytics/dashboard", include_in_schema=False)
async def dashboard_moved():
    """The dashboard is its own app now (a frontend on the platform, rather than
    HTML embedded in this control plane). Keep the old URL working."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(app_url("dashboard"), status_code=308)


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
    exclude_operations=["apps_update_code", "apps_refresh", "apps_frontend_deploy",
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

# Tag each tool read-only / write / destructive. The claude.ai connector UI
# groups tools by these annotations (read-only vs makes-changes vs destructive);
# without them fastapi-mcp emits none and everything lands in one "Other tools"
# bucket. Also helps the model gauge a tool's blast radius. Guarded so a types
# rename can't block startup.
try:
    from mcp.types import ToolAnnotations  # noqa: E402

    _READONLY = {"apps_list", "apps_get", "apps_logs", "apps_deploy_workflow",
                 "apps_env_list", "crons_list", "env_list", "github_secrets_list",
                 "analytics_overview", "analytics_timeseries", "analytics_cohorts",
                 "analytics_agents", "analytics_recent", "apps_stats",
                 "apps_redirects_list"}
    _DESTRUCTIVE = {"apps_delete", "apps_detach_db", "apps_env_delete",
                    "crons_delete", "db_run_script", "env_delete",
                    "github_secret_delete"}
    for _tool in getattr(_mcp, "tools", None) or []:
        if _tool.name in _READONLY:
            _tool.annotations = ToolAnnotations(readOnlyHint=True)
        elif _tool.name in _DESTRUCTIVE:
            _tool.annotations = ToolAnnotations(readOnlyHint=False,
                                                destructiveHint=True)
        else:  # create/set/update/adopt/… — changes state, not destructive
            _tool.annotations = ToolAnnotations(readOnlyHint=False)
except Exception:  # pragma: no cover - never block startup on annotations
    pass

_mcp.mount_http()
