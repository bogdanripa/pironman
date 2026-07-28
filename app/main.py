from contextlib import asynccontextmanager
from fastapi import FastAPI

from .db import init_pool, ensure_schema, close_pool
from .routers import apps, crons, query, scaffold, env

SERVER_DESCRIPTION = """\
Deploy and manage web applications on a self-hosted Raspberry Pi 5.

An **app** is one Docker container plus, optionally, one database and any number
of scheduled jobs. Creating an app gives it a public HTTPS URL automatically at
https://<id>-coolify.bogdanripa.com — there is no DNS, certificate or proxy step
to perform.

What you can do here: list what is deployed, create a new app from a docker
image, adopt an app that already exists in Coolify but was made by hand, redeploy
an app onto a new image tag, delete an app, set shared and per-app environment
variables, run SQL or mongosh scripts against an app's own database, and manage
scheduled HTTP calls to an app.

Three rules govern almost every mistake:

1. **Images must be built for linux/arm64.** The host is a Raspberry Pi. An
   amd64 image will pull successfully and then fail to start with an exec format
   error. In a GitHub Actions build this means `platforms: linux/arm64`.
2. **The container must listen on port 80.** Not 3000, not 8080. Anything else
   returns 502 from the proxy.
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

Deploying new code for an existing app is normally automated: a GitHub Action
builds an arm64 image, pushes it to ghcr.io, then calls apps_update_code with the
new tag. Creating the app itself is a one-off you do here. Do not write that
workflow from memory — call apps_deploy_workflow and use exactly what it returns,
since the tag scheme, platform flag and redeploy call all have to match what
this platform expects.

The whole platform runs on one small machine at home. Deleting an app destroys
its database and all its data with no undo, and running a database script is an
unguarded pipe into that database — confirm both with the user first.\
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_pool()
    await ensure_schema()  # create env tables if missing — no manual SQL on the Pi
    yield
    await close_pool()


app = FastAPI(
    title="paas-api",
    description=SERVER_DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def promote_key(request, call_next):
    """claude.ai custom connectors cannot send an Authorization header, so allow
    the api key as ?key= on /mcp and promote it into the header. The key ends up
    in the connector URL and in edge logs — acceptable for a single-user control
    plane, but use a dedicated key for it so it can be revoked on its own."""
    if request.url.path.startswith("/mcp") and "authorization" not in request.headers:
        key = request.query_params.get("key")
        if key:
            request.scope["headers"] = list(request.scope["headers"]) + [
                (b"authorization", f"Bearer {key}".encode())
            ]
    return await call_next(request)


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


@app.get("/health", tags=["meta"], operation_id="health", include_in_schema=False)
async def health():
    return {"ok": True}


# MCP: the same endpoints exposed as tools at /mcp. Mounted in-process so it
# shares this app's auth and lifespan. Tool names come from operation_id,
# descriptions from each endpoint's summary and docstring.
from fastapi_mcp import FastApiMCP  # noqa: E402

_mcp = FastApiMCP(
    app,
    name="pironman",
    description=SERVER_DESCRIPTION,
)
_mcp.mount_http()
