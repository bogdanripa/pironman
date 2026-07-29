import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from fastapi import FastAPI

from .db import init_pool, ensure_schema, close_pool
from . import autoupdate
from .routers import apps, crons, query, scaffold, env, refresh


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

What you can do here: list what is deployed, create a new app from a docker
image, adopt an app that already exists in Coolify but was made by hand, change
an app's image, read an app's container logs and status, delete an app, set
shared and per-app environment variables, run SQL or mongosh scripts against an
app's own database, and manage scheduled HTTP calls to an app.

Three rules govern almost every mistake:

1. **Images must be built for linux/arm64.** The host is a Raspberry Pi. An
   amd64 image will pull successfully and then fail to start with an exec format
   error. In a GitHub Actions build this means `platforms: linux/arm64`.
2. **The container must listen on port 80, bound to `::` (dual-stack).** Not
   3000/8080 (→ 502 from the proxy), and not `0.0.0.0` alone — the in-container
   healthcheck hits `localhost`, which resolves to IPv6 `::1` first, so an
   IPv4-only bind fails the check and the deploy is rolled back while the app
   looks fine from outside. Port 80 is privileged, so run as root (no `USER`
   line). When a deploy 'succeeds' but the URL 502s, apps_logs shows why.
3. **App ids become hostnames.** Lowercase, alphanumeric and hyphens only, one
   flat label, fixed once created.

Databases are per-app and provisioned on request. The connection string is
injected into the container as the DATABASE_URL environment variable on every
deploy and recomposed each time, so an app should read it from the environment
rather than hardcoding anything.

Apps also take environment variables in two scopes. **Shared** variables are set
once and injected into every app — the place for account-wide secrets such as an
OpenAI API key, since this is a single owner's box. **App-specific** variables
apply to one app and override a shared variable of the same name. Setting or
removing a variable redeploys the affected app(s) so it reaches the running
container; a shared change therefore redeploys everything. Values are write-only:
you can set and overwrite them but never read one back — listings show a masked
preview only, so re-set a variable if you are unsure of its value.

Deploys go through CI, not through hand-built images, and they need no secret.
The right way to build and deploy an app is to wire its repository to GitHub
Actions: each push to main builds the arm64 image, pushes it to ghcr.io tagged
:latest, and calls the app's unauthenticated /refresh hook. The box watches that
tag and redeploys the new image — on the /refresh call, and hourly regardless —
so there is deliberately no tool here to deploy an app by hand. Do NOT build an
image locally and push it to the registry yourself, and do not go looking for
registry or deploy credentials to do so — that is the wrong path, it will not
match the arm64 platform this platform expects, and it does not reproduce. To
set this up, call apps_deploy_workflow and write exactly what it returns into the
app's repo; to ship a change after that, commit and push. Building an image by
hand is only ever a first-time bootstrap step for the control plane itself.

Creating the app itself is the one-off you do here, before its first CI run.
apps_create and apps_deploy_workflow each return an ordered deploy runbook —
follow it to wire up CI and ship the app end to end yourself, with no repository
secret and no human step. New apps auto-update by default; apps_autoupdate turns
it off (e.g. to hold a manual rollback).

The whole platform runs on one small machine at home. Deleting an app destroys
its database and all its data with no undo, and running a database script is an
unguarded pipe into that database — confirm both with the user first.\
"""


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_pool()
    await ensure_schema()  # create env tables if missing — no manual SQL on the Pi
    task = asyncio.create_task(_autoupdate_loop())
    yield
    task.cancel()
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
    exclude_operations=["apps_update_code", "apps_refresh"],
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

_mcp.mount_http()
