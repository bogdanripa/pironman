from contextlib import asynccontextmanager
from fastapi import FastAPI

from .db import init_pool, close_pool
from .routers import apps, crons, query


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_pool()
    yield
    await close_pool()


app = FastAPI(
    title="paas-api",
    description="Control plane for the Pironman box. An app = 1 container "
                "+ 0..1 database + a public URL + cron jobs.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(apps.router)
app.include_router(query.router)
app.include_router(crons.router)


@app.get("/health", tags=["meta"], operation_id="health")
async def health():
    return {"ok": True}


# MCP: exposes the same endpoints as tools at /mcp. Mounted in-process, so it
# shares this app's auth — clients pass the same bearer key.
from fastapi_mcp import FastApiMCP  # noqa: E402

_mcp = FastApiMCP(
    app,
    name="pironman-paas",
    description=(
        "Deploy and manage apps on Bogdan's Raspberry Pi. An app is one arm64 "
        "docker container listening on port 80, reachable at "
        "https://<id>-coolify.bogdanripa.com, optionally with a postgres or "
        "mongo database and scheduled HTTP jobs."
    ),
)
_mcp.mount_http()
