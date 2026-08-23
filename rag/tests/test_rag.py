"""Exercise the RAG service end to end.

Run directly: `python rag/tests/test_rag.py`
Needs fastapi, httpx, numpy, selectolax, pypdf.

The embedding model is replaced with a deterministic bag-of-words stand-in so
the suite needs no 30MB download and no network. That is deliberate: what is
under test here is storage, capability handling, truncation reporting and the
SSRF guard. Embedding quality is a separate question and was measured on the
box, not asserted here.
"""
import os
import re
import sys
import tempfile
import zlib
from pathlib import Path

os.environ["RAG_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.sqlite3")
os.environ["RAG_INDEX_LIMIT_TOKENS"] = "120"      # small, so truncation is testable
os.environ["RAG_CHUNK_TOKENS"] = "20"
os.environ["RAG_CHUNK_OVERLAP_TOKENS"] = "4"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rag import embed, extract, fetch, server  # noqa: E402


class FakeModel:
    """Hashed bag of words: similar text gets similar vectors, so cosine means
    something, without loading a real model."""
    dim = 64

    def encode(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", str(text).lower()):
                out[i, zlib.crc32(word.encode()) % self.dim] += 1.0
        return out


embed._model = FakeModel()

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def wait_ready(client, doc_id, tries=100):
    for _ in range(tries):
        body = client.post("/documents/status", json={"ids": [doc_id]}).json()
        doc = body["documents"][0]
        if doc["status"] in ("ready", "failed", "unknown"):
            return doc
    return {"status": "timeout"}


def main():
    with TestClient(server.app) as client:
        print("\n[ingest and query]")
        text = ("The termination clause allows either party to end the "
                "agreement with thirty days written notice.\n\n"
                "Payment terms are net thirty from invoice date.\n\n"
                "Governing law is the law of Romania.")
        doc = client.post("/documents", json={"text": text, "filename": "c.md"}).json()
        check("ingest returns 202 with a pending id",
              doc["status"] == "pending" and len(doc["id"]) == 16, doc)
        ready = wait_ready(client, doc["id"])
        check("document becomes ready", ready["status"] == "ready", ready)

        res = client.post("/query", json={"ids": [doc["id"]], "q": "termination notice"}).json()
        check("query finds the termination text",
              any("termination" in r["text"].lower() for r in res["results"]), res)
        check("results carry the document id",
              all(r["id"] == doc["id"] for r in res["results"]))

        print("\n[a query naming no document returns nothing]")
        other = client.post("/query", json={"ids": ["zzzzzzzzzzzzzzzz"], "q": "termination"}).json()
        check("unknown id yields no results", other["results"] == [], other)

        print("\n[punctuation must not crash FTS]")
        for q in ["what's the term?", "net-thirty (30)", '"quoted" AND OR *']:
            r = client.post("/query", json={"ids": [doc["id"]], "q": q})
            check(f"query {q!r} is handled", r.status_code == 200, r.text[:80])

        print("\n[dedup shares storage but not the capability]")
        second = client.post("/documents", json={"text": text, "filename": "c.md"}).json()
        wait_ready(client, second["id"])
        check("same bytes get a DIFFERENT document id", second["id"] != doc["id"],
              f"{doc['id']} vs {second['id']}")
        import sqlite3
        from rag.config import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT content_hash, refcount FROM contents").fetchall()
        check("stored once, referenced twice",
              len(rows) == 1 and rows[0][1] == 2, rows)

        print("\n[deleting one document leaves the other intact]")
        client.post("/documents/delete", json={"ids": [doc["id"]]})
        gone = client.post("/documents/status", json={"ids": [doc["id"]]}).json()["documents"][0]
        check("deleted id is unknown", gone["status"] == "unknown", gone)
        still = client.post("/query", json={"ids": [second["id"]], "q": "termination"}).json()
        check("the other document still answers", len(still["results"]) > 0, still)
        rows = conn.execute("SELECT refcount FROM contents").fetchall()
        check("refcount dropped to 1, content kept", rows and rows[0][0] == 1, rows)

        print("\n[truncation is reported at QUERY time, not just ingest]")
        long_text = "\n\n".join(
            f"Paragraph {i} about assorted contractual matters and clauses." for i in range(80))
        big = client.post("/documents", json={"text": long_text, "filename": "big.md"}).json()
        ready = wait_ready(client, big["id"])
        check("long document indexes", ready["status"] == "ready", ready)
        check("status reports truncation", "truncated" in ready, ready)
        qres = client.post("/query", json={"ids": [big["id"]], "q": "contractual"}).json()
        check("QUERY reports truncation too",
              qres["truncated"] and qres["truncated"][0]["id"] == big["id"], qres)

        print("\n[a document that yields no text fails, it does not sit at ready]")
        empty = client.post("/documents", json={"text": "   \n  \n "}).json()
        got = wait_ready(client, empty["id"])
        check("empty document is failed with a reason",
              got["status"] == "failed" and got.get("error"), got)

        print("\n[capability ids never appear in a route path]")
        paths = [r.path for r in server.app.routes]
        check("no route takes a path parameter",
              not any("{" in p for p in paths), paths)

    print("\n[SSRF guard]")
    for bad in ["http://127.0.0.1/x", "http://localhost/x", "http://[::1]/x",
                "http://10.0.1.10:10000/api/instances", "http://169.254.169.254/",
                "file:///etc/passwd", "gopher://x/", "http://user:pw@example.com/"]:
        try:
            fetch._validate(bad)
            check(f"refuses {bad}", False, "was ALLOWED")
        except fetch.FetchError:
            check(f"refuses {bad}", True)
    check("rejects IPv6 unique-local (this box's overlay)",
          not fetch._is_public("fddf:6e69:23a7::4"))
    check("rejects IPv4 private", not fetch._is_public("10.0.1.15"))
    check("accepts a public address", fetch._is_public("1.1.1.1"))

    print("\n[a sign-in page is not a document]")
    try:
        fetch.guard_login_page(b"<html><title>Sign in - Google Accounts</title>", "https://x/")
        check("google sign-in page refused", False, "was ACCEPTED")
    except fetch.FetchError:
        check("google sign-in page refused", True)

    print("\n[scanned PDF fails loudly rather than indexing nothing]")
    try:
        from pypdf import PdfWriter
        import io
        w = PdfWriter()
        for _ in range(3):
            w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        try:
            extract.extract(buf.getvalue(), "scan.pdf")
            check("blank/scanned PDF is refused", False, "was ACCEPTED")
        except extract.ExtractError as exc:
            check("blank/scanned PDF is refused", "scanned" in str(exc) or "no text" in str(exc), str(exc)[:70])
    except ImportError:
        print("  SKIP  pypdf not installed")

    print("\n[format sniffing ignores the filename]")
    check("a PDF called .txt is still a PDF",
          extract.sniff(b"%PDF-1.7\n...", "notes.txt") == "application/pdf")
    check("html is detected", extract.sniff(b"<!DOCTYPE html><html>", None) == "text/html")

    print("\n" + ("ALL PASS" if not FAILS else f"FAILURES: {FAILS}"))
    return 1 if FAILS else 0


sys.exit(main())
