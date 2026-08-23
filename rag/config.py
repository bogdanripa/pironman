"""Limits and settings for the RAG service.

Every number here is a policy choice rather than a constant of nature, so all of
them are env-overridable: raising a paying customer's index limit is a setting,
not a rebuild.

The split that matters is EXTRACT versus INDEX. Extraction is the cheap step —
parsing text out of a file — and we keep all of it, so raising someone's index
limit later re-indexes text we already hold instead of asking them to upload the
contract again. Embedding is the step that costs, so the index cap is where the
money is actually saved. Measured on this box: 0.18 ms to embed a chunk, so the
cap exists to bound storage and abuse, not because embedding is slow.
"""
import json
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Ingress. These bound bytes we accept, and nothing here is ever stored: the
# upload streams to a temp file, gets extracted, and the file is deleted. So
# these are the size of a transient buffer, not a storage commitment.
MAX_UPLOAD_BYTES = _int("RAG_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
MAX_FETCH_BYTES = _int("RAG_MAX_FETCH_BYTES", 25 * 1024 * 1024)
FETCH_TIMEOUT_S = _int("RAG_FETCH_TIMEOUT_S", 20)

# Extraction: capped only so the cheap step cannot become the expensive one. A
# 2000-page PDF costs real CPU to parse even when we embed 20 pages of it.
MAX_EXTRACT_PAGES = _int("RAG_MAX_EXTRACT_PAGES", 500)
MAX_EXTRACT_CHARS = _int("RAG_MAX_EXTRACT_CHARS", 5 * 1024 * 1024)

# Indexing: the tier boundary. Pages exist only for PDF and PPTX -- DOCX has no
# page count until something renders it, and HTML/MD/TXT have none at all -- so
# tokens are the canonical budget and pages are a secondary cap applied only
# where they are real. Whichever binds first wins.
INDEX_LIMIT_TOKENS = _int("RAG_INDEX_LIMIT_TOKENS", 13000)  # ~20 pages of prose
INDEX_LIMIT_PAGES = _int("RAG_INDEX_LIMIT_PAGES", 20)

CHUNK_TOKENS = _int("RAG_CHUNK_TOKENS", 400)
CHUNK_OVERLAP_TOKENS = _int("RAG_CHUNK_OVERLAP_TOKENS", 60)

# Tokens are estimated as characters/4 rather than tokenized for real. The
# estimate only sizes chunks and caps, and being 20% out moves a boundary rather
# than breaking anything -- while running the tokenizer twice over every
# document to be exact would cost more than the cap saves.
CHARS_PER_TOKEN = 4

# A scanned PDF extracts almost nothing. Indexing it would create a document
# that reports ready and answers every query with silence, which is the failure
# mode this whole platform keeps meeting: a failure dressed as a success. So it
# fails ingest instead, with a reason the caller can act on.
MIN_CHARS_PER_PAGE = _int("RAG_MIN_CHARS_PER_PAGE", 30)

MODEL_PATH = os.environ.get("RAG_MODEL_PATH", "/srv/model")
DB_PATH = os.environ.get("RAG_DB_PATH", "/data/rag.sqlite3")

# 16 alphanumerics is ~95 bits from a CSPRNG. Guessing is not the threat model;
# leakage is, which is why these never travel in a URL path or query string --
# this platform's own edge proxy logs full request lines, and an app's secret
# key has already been read out of that log once.
DOC_ID_LEN = 16

# Optional per-tier overrides, keyed by the X-Tier-Key header on ingest only.
# Reads stay keyless: a document ID is the whole capability. Shipping the hook
# now costs nothing; retrofitting identity into an anonymous ingest path later
# means either breaking clients or running two endpoints.
#   RAG_TIERS='{"somekey": {"index_limit_tokens": 100000, "index_limit_pages": 200}}'
try:
    TIERS = json.loads(os.environ.get("RAG_TIERS", "{}"))
except ValueError:
    TIERS = {}


def limits_for(tier_key: str | None) -> dict:
    """Index limits for this caller. Unknown or absent key -> free tier."""
    tier = TIERS.get(tier_key or "", {})
    return {
        "index_limit_tokens": int(tier.get("index_limit_tokens", INDEX_LIMIT_TOKENS)),
        "index_limit_pages": int(tier.get("index_limit_pages", INDEX_LIMIT_PAGES)),
        "tier": tier_key if tier else None,
    }
