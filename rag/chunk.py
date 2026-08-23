"""Split extracted text into chunks, respecting a token budget.

Chunk boundaries follow the document's own structure where it has any: a
paragraph break is a better boundary than a fixed offset, and a heading is
better still, because a chunk that starts mid-sentence retrieves badly and reads
worse when it is handed back as a citation.
"""
from .config import CHARS_PER_TOKEN, CHUNK_OVERLAP_TOKENS, CHUNK_TOKENS


def est_tokens(text: str) -> int:
    """Characters/4. See config: an estimate is enough to size a chunk."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in text.split("\n\n")]
    return [p for p in parts if p]


def chunk_blocks(blocks: list[dict], limit_tokens: int) -> tuple[list[dict], bool]:
    """Blocks -> chunks, stopping at limit_tokens.

    A block is {"text": str, "page": int|None, "heading": str|None} as produced
    by extraction, so page and heading survive into each chunk and can be
    returned with a hit -- "page 14" is worth far more to a caller than an
    offset into a blob.

    Returns (chunks, truncated). `truncated` is the whole point of the return
    value: the caller has to report it, because a silently shortened document
    answers queries about its missing half with confident nothing.
    """
    target = CHUNK_TOKENS * CHARS_PER_TOKEN
    overlap = CHUNK_OVERLAP_TOKENS * CHARS_PER_TOKEN
    budget = limit_tokens * CHARS_PER_TOKEN

    chunks: list[dict] = []
    used = 0
    truncated = False

    for block in blocks:
        if used >= budget:
            truncated = True
            break
        for para in _split_paragraphs(block.get("text", "")):
            if used >= budget:
                truncated = True
                break
            # A paragraph longer than the target is cut on a window with
            # overlap, so a sentence spanning the cut still appears whole in one
            # of the two chunks.
            start = 0
            while start < len(para):
                if used >= budget:
                    truncated = True
                    break
                piece = para[start:start + target]
                room = budget - used
                if len(piece) > room:
                    piece = piece[:room]
                    truncated = True
                chunks.append({
                    "text": piece,
                    "page": block.get("page"),
                    "heading": block.get("heading"),
                })
                used += len(piece)
                if start + target >= len(para):
                    break
                start += max(1, target - overlap)

    return chunks, truncated
