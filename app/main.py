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


@app.get("/health", tags=["meta"])
async def health():
    return {"ok": True}
