"""SQLite storage and hybrid retrieval.

SQLite rather than Postgres for three reasons that all matter here: pgvector is
not available on this box's Postgres (18.4 on musl, only pg_trgm offered), an
embedded file has no resident daemon competing for four already-busy cores, and
it opens instantly -- which counts because this service scales to zero and pays
its startup in front of a waiting caller.

Storage is content-addressed and the capability is not. Identical bytes are
stored, chunked and embedded once under their hash; every upload still mints its
own random document ID pointing at that content, refcounted. Two clients who
upload the same PDF therefore share the storage and neither can reach the
other's document -- which a naive "same hash, same ID" dedup would break by
handing the second uploader a live capability belonging to the first.
"""
import os
import secrets
import sqlite3
import threading
import time
import zlib

import numpy as np

from . import embed
from .config import DOC_ID_LEN, MODEL_PATH

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS contents (
  content_hash   TEXT PRIMARY KEY,
  refcount       INTEGER NOT NULL DEFAULT 0,
  text_z         BLOB,
  total_pages    INTEGER,
  total_tokens   INTEGER NOT NULL DEFAULT 0,
  indexed_pages  INTEGER,
  indexed_tokens INTEGER NOT NULL DEFAULT 0,
  truncated      INTEGER NOT NULL DEFAULT 0,
  chunk_count    INTEGER NOT NULL DEFAULT 0,
  dim            INTEGER NOT NULL DEFAULT 0,
  created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id           TEXT PRIMARY KEY,
  content_hash TEXT,
  status       TEXT NOT NULL,
  error        TEXT,
  title        TEXT,
  mime         TEXT,
  source_url   TEXT,
  bytes        INTEGER,
  tier         TEXT,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id           INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL,
  ord          INTEGER NOT NULL,
  page         INTEGER,
  heading      TEXT,
  text         TEXT NOT NULL,
  vec          BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_content ON chunks(content_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
  USING fts5(text, content='chunks', content_rowid='id');

-- External-content FTS5 keeps no copy of the text, so it must be told about
-- every write. Triggers rather than call sites: a delete path that forgets the
-- 'delete' command leaves the index matching rows that no longer exist, and
-- that shows up as a phantom hit long after the code that caused it.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
"""


def connect(path: str) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            _conn = sqlite3.connect(path, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def new_doc_id() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(DOC_ID_LEN))


def create_pending(conn, source_url: str | None, tier: str | None) -> str:
    doc_id = new_doc_id()
    now = time.time()
    with _lock:
        conn.execute(
            "INSERT INTO documents (id, status, source_url, tier, created_at, updated_at)"
            " VALUES (?, 'pending', ?, ?, ?, ?)",
            (doc_id, source_url, tier, now, now))
        conn.commit()
    return doc_id


def get_content(conn, content_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contents WHERE content_hash = ?", (content_hash,)).fetchone()


def store_content(conn, content_hash: str, text: str, stats: dict,
                  chunks: list[dict], vectors: np.ndarray) -> None:
    """Write content plus its chunks, once. Callers check get_content first;
    the INSERT OR IGNORE makes a lost race harmless rather than a duplicate."""
    dim = int(vectors.shape[1]) if vectors.size else 0
    with _lock:
        cur = conn.execute(
            "INSERT OR IGNORE INTO contents (content_hash, refcount, text_z,"
            " total_pages, total_tokens, indexed_pages, indexed_tokens,"
            " truncated, chunk_count, dim, created_at)"
            " VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content_hash, zlib.compress(text.encode("utf-8"), 6),
             stats.get("total_pages"), stats.get("total_tokens", 0),
             stats.get("indexed_pages"), stats.get("indexed_tokens", 0),
             1 if stats.get("truncated") else 0, len(chunks), dim, time.time()))
        if cur.rowcount:
            conn.executemany(
                "INSERT INTO chunks (content_hash, ord, page, heading, text, vec)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(content_hash, i, c.get("page"), c.get("heading"), c["text"],
                  embed.quantize(vectors[i:i + 1]))
                 for i, c in enumerate(chunks)])
        conn.commit()


def attach(conn, doc_id: str, content_hash: str, meta: dict) -> None:
    """Point a document at content and take a reference on it."""
    with _lock:
        conn.execute(
            "UPDATE documents SET content_hash = ?, status = 'ready', error = NULL,"
            " title = ?, mime = ?, bytes = ?, updated_at = ? WHERE id = ?",
            (content_hash, meta.get("title"), meta.get("mime"),
             meta.get("bytes"), time.time(), doc_id))
        conn.execute(
            "UPDATE contents SET refcount = refcount + 1 WHERE content_hash = ?",
            (content_hash,))
        conn.commit()


def mark_failed(conn, doc_id: str, error: str) -> None:
    with _lock:
        conn.execute(
            "UPDATE documents SET status = 'failed', error = ?, updated_at = ?"
            " WHERE id = ?", (error[:500], time.time(), doc_id))
        conn.commit()


def status(conn, ids: list[str]) -> list[dict]:
    out = []
    for doc_id in ids:
        row = conn.execute(
            "SELECT d.*, c.total_pages, c.total_tokens, c.indexed_pages,"
            " c.indexed_tokens, c.truncated, c.chunk_count"
            " FROM documents d LEFT JOIN contents c ON c.content_hash = d.content_hash"
            " WHERE d.id = ?", (doc_id,)).fetchone()
        if row is None:
            # Indistinguishable from a wrong ID on purpose: confirming that an
            # ID exists but is not yours would turn the endpoint into an oracle
            # for probing the ID space.
            out.append({"id": doc_id, "status": "unknown"})
            continue
        item = {"id": doc_id, "status": row["status"], "title": row["title"],
                "mime": row["mime"], "source_url": row["source_url"],
                "bytes": row["bytes"], "chunks": row["chunk_count"]}
        if row["status"] == "failed":
            item["error"] = row["error"]
        if row["truncated"]:
            item["truncated"] = {
                "indexed_pages": row["indexed_pages"], "total_pages": row["total_pages"],
                "indexed_tokens": row["indexed_tokens"], "total_tokens": row["total_tokens"]}
        out.append(item)
    return out


def delete(conn, ids: list[str]) -> dict:
    deleted = 0
    with _lock:
        for doc_id in ids:
            row = conn.execute(
                "SELECT content_hash FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if row is None:
                continue
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            deleted += 1
            h = row["content_hash"]
            if not h:
                continue
            conn.execute(
                "UPDATE contents SET refcount = refcount - 1 WHERE content_hash = ?", (h,))
            # Content outlives the document that created it for as long as any
            # other document still points at it -- which is the point of
            # refcounting, and why one client deleting cannot destroy another's.
            left = conn.execute(
                "SELECT refcount FROM contents WHERE content_hash = ?", (h,)).fetchone()
            if left is not None and left["refcount"] <= 0:
                conn.execute("DELETE FROM chunks WHERE content_hash = ?", (h,))
                conn.execute("DELETE FROM contents WHERE content_hash = ?", (h,))
        conn.commit()
    return {"deleted": deleted}


def _fts_query(q: str) -> str:
    """User text -> a safe FTS5 MATCH expression.

    FTS5 treats bare punctuation as syntax, so an unescaped question mark or
    hyphen raises OperationalError and takes the whole query down. Terms are
    extracted and quoted individually, which cannot be malformed regardless of
    what arrives.
    """
    terms = []
    word = []
    for ch in q:
        if ch.isalnum():
            word.append(ch)
        elif word:
            terms.append("".join(word))
            word = []
    if word:
        terms.append("".join(word))
    return " OR ".join('"%s"' % t for t in terms if t)


def search(conn, ids: list[str], query: str, k: int, rrf_k: int = 60) -> dict:
    """Hybrid retrieval over the named documents only.

    BM25 and cosine are combined by Reciprocal Rank Fusion, which uses each
    engine's RANK rather than its score. That matters because the two produce
    incomparable numbers -- SQLite's bm25() is an unbounded negative, cosine is
    [-1, 1] -- and any attempt to put them on one scale needs a calibration that
    drifts with corpus and query. Ranks need none.
    """
    rows = conn.execute(
        "SELECT id, content_hash, status FROM documents WHERE id IN (%s)"
        % ",".join("?" * len(ids)), ids).fetchall() if ids else []
    hash_to_doc: dict[str, str] = {}
    for r in rows:
        if r["status"] == "ready" and r["content_hash"]:
            hash_to_doc.setdefault(r["content_hash"], r["id"])
    hashes = list(hash_to_doc)
    if not hashes:
        return {"results": [], "truncated": [], "searched": []}

    place = ",".join("?" * len(hashes))
    pool = max(k * 5, 50)

    fts_ranked: list[int] = []
    expr = _fts_query(query)
    if expr:
        fts_ranked = [r["id"] for r in conn.execute(
            "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid"
            " WHERE chunks_fts MATCH ? AND c.content_hash IN (%s)"
            " ORDER BY bm25(chunks_fts) LIMIT ?" % place,
            [expr, *hashes, pool]).fetchall()]

    vec_rows = conn.execute(
        "SELECT id, vec FROM chunks WHERE content_hash IN (%s) ORDER BY id" % place,
        hashes).fetchall()
    vec_ranked: list[int] = []
    if vec_rows:
        dim = conn.execute(
            "SELECT dim FROM contents WHERE content_hash = ?", (hashes[0],)).fetchone()["dim"]
        matrix = embed.dequantize(b"".join(r["vec"] for r in vec_rows), dim)
        qv = embed.encode(MODEL_PATH, [query])[0]
        scores = embed.similarities(matrix, qv)
        order = np.argsort(-scores)[:pool]
        vec_ranked = [vec_rows[i]["id"] for i in order]

    fused: dict[int, float] = {}
    for ranking in (fts_ranked, vec_ranked):
        for rank, chunk_id in enumerate(ranking):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank + 1)
    top = sorted(fused, key=lambda c: -fused[c])[:k]

    results = []
    for chunk_id in top:
        row = conn.execute(
            "SELECT content_hash, ord, page, heading, text FROM chunks WHERE id = ?",
            (chunk_id,)).fetchone()
        results.append({
            "id": hash_to_doc.get(row["content_hash"]),
            "ord": row["ord"], "page": row["page"], "heading": row["heading"],
            "text": row["text"], "score": round(fused[chunk_id], 6),
        })

    # Truncation is reported HERE, not only at ingest. The caller of /query is
    # often not the uploader and has never seen the ingest status, so without
    # this a query about page 150 of a 200-page contract returns a confident,
    # well-ranked, wrong answer with nothing to suggest four fifths of the
    # document was never indexed.
    truncated = []
    for h, doc_id in hash_to_doc.items():
        c = conn.execute(
            "SELECT * FROM contents WHERE content_hash = ? AND truncated = 1", (h,)).fetchone()
        if c:
            truncated.append({
                "id": doc_id, "indexed_pages": c["indexed_pages"],
                "total_pages": c["total_pages"], "indexed_tokens": c["indexed_tokens"],
                "total_tokens": c["total_tokens"]})
    return {"results": results, "truncated": truncated,
            "searched": sorted(hash_to_doc.values())}
