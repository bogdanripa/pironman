"""The RAG service: ingest documents, query them by ID.

Two rules shape the whole API surface.

**Document IDs never appear in a URL.** They are the only credential this
service has, and this platform's edge proxy logs full request lines into an
access log that analytics then ingests -- an app's secret key has already been
read out of that log once. So every endpoint that names a document is a POST
that carries the IDs in its body, including status and delete, which REST would
put in the path.

**Ingest is asynchronous.** Fetching, parsing and embedding a large document can
outlast the 100-second edge timeout, and a caller who receives a 524 for work
that actually succeeded will retry -- creating a second document for the same
content. So POST /documents returns 202 immediately with a real ID, and the work
happens behind it.
"""
import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import chunk as chunker
from . import embed, extract, fetch, store
from .config import DB_PATH, MAX_UPLOAD_BYTES, MODEL_PATH, limits_for

_log = logging.getLogger("rag")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

QUEUE: asyncio.Queue = asyncio.Queue()


class IngestBody(BaseModel):
    text: str | None = None
    base64: str | None = None
    url: str | None = None
    filename: str | None = None


class IdsBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


class QueryBody(BaseModel):
    ids: list[str] = Field(default_factory=list)
    q: str
    k: int = 8


def _ingest(doc_id: str, payload: dict, limits: dict) -> None:
    """Do the work for one document. Runs in a worker thread.

    Every failure path ends at mark_failed with a readable reason. A document
    that cannot be read must not sit at 'ready' with nothing in it -- that turns
    a broken upload into a document which answers every question with silence.
    """
    conn = store.connect(DB_PATH)
    try:
        source_url = payload.get("url")
        if source_url:
            data, final_url, _ctype = fetch.fetch(source_url)
            fetch.guard_login_page(data, final_url)
            filename = payload.get("filename") or final_url
        else:
            data = payload["data"]
            filename = payload.get("filename")

        if len(data) > MAX_UPLOAD_BYTES:
            raise extract.ExtractError(
                "document is %d bytes, over the %d byte limit"
                % (len(data), MAX_UPLOAD_BYTES))

        # Dedup on the bytes as received. The hash is stored; the bytes are not
        # -- once extracted they are dropped, so the only thing that grows with
        # use is compressed text and vectors.
        content_hash = hashlib.sha256(data).hexdigest()[:32]

        existing = store.get_content(conn, content_hash)
        if existing is None:
            parsed = extract.extract(data, filename)
            chunks, truncated = chunker.chunk_blocks(
                parsed["blocks"], limits["index_limit_tokens"])
            pages = [c["page"] for c in chunks if c.get("page")]
            limit_pages = limits["index_limit_pages"]
            if pages and limit_pages and max(pages) > limit_pages:
                chunks = [c for c in chunks
                          if not c.get("page") or c["page"] <= limit_pages]
                truncated = True
            if not chunks:
                raise extract.ExtractError("nothing left to index after chunking")
            vectors = embed.encode(MODEL_PATH, [c["text"] for c in chunks])
            indexed_pages = max((c["page"] for c in chunks if c.get("page")),
                                default=None)
            store.store_content(conn, content_hash, parsed["text"], {
                "total_pages": parsed["total_pages"],
                "total_tokens": parsed["total_tokens"],
                "indexed_pages": indexed_pages,
                "indexed_tokens": sum(chunker.est_tokens(c["text"]) for c in chunks),
                "truncated": truncated,
            }, chunks, vectors)
            meta = {"title": parsed["title"], "mime": parsed["mime"],
                    "bytes": len(data)}
        else:
            # Same bytes as something already indexed: reuse the storage, mint a
            # separate capability. The second uploader must not receive the
            # first uploader's document ID.
            meta = {"title": payload.get("filename"), "mime": None,
                    "bytes": len(data)}

        store.attach(conn, doc_id, content_hash, meta)
        _log.info("indexed %s (%s)", doc_id, content_hash[:8])
    except (fetch.FetchError, extract.ExtractError) as exc:
        store.mark_failed(conn, doc_id, str(exc))
        _log.info("ingest failed for %s: %s", doc_id, exc)
    except Exception as exc:  # noqa: BLE001 - a worker that dies stops everything
        store.mark_failed(conn, doc_id, "internal error: %s" % exc)
        _log.exception("ingest crashed for %s", doc_id)


async def _worker() -> None:
    while True:
        doc_id, payload, limits = await QUEUE.get()
        try:
            await asyncio.to_thread(_ingest, doc_id, payload, limits)
        finally:
            QUEUE.task_done()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    store.connect(DB_PATH)
    # Load the model at startup rather than on the first query: it takes 0.12s
    # from the baked-in copy, and paying that inside a request would put it in
    # front of a caller who is already waiting out a cold wake.
    with contextlib.suppress(Exception):
        embed.load(MODEL_PATH)
    task = asyncio.create_task(_worker())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        store.close()


app = FastAPI(title="pironman-rag", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "queued": QUEUE.qsize()}


@app.post("/documents", status_code=202)
async def ingest(body: IngestBody, x_tier_key: str | None = Header(default=None)):
    given = [f for f in (body.text, body.base64, body.url) if f]
    if len(given) != 1:
        raise HTTPException(400, "send exactly one of text, base64 or url")

    payload: dict = {"filename": body.filename}
    if body.url:
        payload["url"] = body.url
    elif body.text is not None:
        payload["data"] = body.text.encode("utf-8")
    else:
        try:
            payload["data"] = base64.b64decode(body.base64 or "", validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "base64 field is not valid base64")

    if "data" in payload and len(payload["data"]) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "document exceeds %d bytes" % MAX_UPLOAD_BYTES)

    limits = limits_for(x_tier_key)
    conn = store.connect(DB_PATH)
    doc_id = store.create_pending(conn, body.url, limits["tier"])
    await QUEUE.put((doc_id, payload, limits))
    return {"id": doc_id, "status": "pending",
            "index_limit_tokens": limits["index_limit_tokens"],
            "index_limit_pages": limits["index_limit_pages"]}


@app.post("/documents/status")
async def documents_status(body: IdsBody):
    conn = store.connect(DB_PATH)
    return {"documents": store.status(conn, body.ids[:200])}


@app.post("/documents/delete")
async def documents_delete(body: IdsBody):
    conn = store.connect(DB_PATH)
    return store.delete(conn, body.ids[:200])


@app.post("/query")
async def query(body: QueryBody):
    if not body.q.strip():
        raise HTTPException(400, "q must not be empty")
    if not body.ids:
        raise HTTPException(400, "ids must name at least one document")
    conn = store.connect(DB_PATH)
    return await asyncio.to_thread(
        store.search, conn, body.ids[:100], body.q, max(1, min(body.k, 50)))
