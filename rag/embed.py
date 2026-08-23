"""Static embeddings, quantized to int8.

A Model2Vec static model rather than a transformer, and the reason is measured
on the box this runs on (Raspberry Pi 5, 4 cores, already at load ~2):

    warm load from the baked-in model : 0.12 s
    embed one long chunk              : 0.18 ms
    model on disk                     : 30 MB, 256 dims, no torch

A transformer would cost 10-30 ms per chunk and pull a runtime that dominates
the image. The 0.12 s load matters more than it looks: this service scales to
zero, so model load is paid on every cold wake, in front of a waiting caller.

Static embeddings are weaker than a real encoder on subtle paraphrase. That is
the trade, and it is why retrieval is hybrid: BM25 carries the exact-term half
that embeddings are worst at, and RRF fuses them.
"""
import numpy as np

_model = None


def load(path: str):
    """Load once, lazily. Import is deferred so tests and tooling that never
    embed anything do not pay for it."""
    global _model
    if _model is None:
        from model2vec import StaticModel
        _model = StaticModel.from_pretrained(path)
    return _model


def dims(path: str) -> int:
    return int(load(path).dim)


def encode(path: str, texts: list[str]) -> np.ndarray:
    """Texts -> L2-normalized float32 vectors, one row each."""
    if not texts:
        return np.zeros((0, dims(path)), dtype=np.float32)
    v = np.asarray(load(path).encode(texts), dtype=np.float32)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    # A chunk that embeds to all zeros (e.g. nothing but punctuation) would
    # divide by zero and poison the matrix with NaN, which then ranks
    # unpredictably rather than simply scoring badly.
    norms[norms == 0] = 1.0
    return v / norms


def quantize(v: np.ndarray) -> bytes:
    """Unit vectors -> int8 blob. Since every row is already L2-normalized,
    scaling by 127 keeps the full range and a dot product of two quantized rows
    is cosine similarity times 127^2 -- a monotonic transform, so ranking is
    unaffected and no dequantization is needed to sort."""
    return np.clip(np.rint(v * 127.0), -127, 127).astype(np.int8).tobytes()


def dequantize(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.int8).reshape(-1, dim).astype(np.float32)


def similarities(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Cosine of every row against one query vector.

    Deliberately brute force: no ANN index, no graph to build, rebuild on
    delete, or hold in memory. Measured here, 20,000 chunks scores in 1.76 ms
    and 2,000 in 0.12 ms -- and queries name their documents, so the scoped set
    is normally in the hundreds. An index would add maintenance and a memory
    floor to save microseconds.
    """
    if matrix.size == 0:
        return np.zeros((0,), dtype=np.float32)
    return matrix @ query
