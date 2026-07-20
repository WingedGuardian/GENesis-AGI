"""Procedure principle embedding — pack/unpack + cosine helpers.

Procedures store `principle_embedding` as a BLOB of 1024 little-endian
float32 values (4096 bytes). Storing the embedding avoids re-embedding
existing principles on every proactive-hook firing.

The hook reads the BLOB, unpacks to a vector, computes cosine vs the
already-embedded prompt vector, and surfaces a procedure when the max
cosine across all procedures crosses the activation threshold.
"""

from __future__ import annotations

import logging
import math
import struct
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from genesis.memory.embeddings import EmbeddingProvider

logger = logging.getLogger(__name__)

# qwen3-embedding output dimensionality. Matches genesis.memory.embeddings.
EMBEDDING_DIM = 1024

# ── Shared novelty-gate state ──────────────────────────────────────────────────
# Both procedure-extraction paths (learning/procedural/extractor.py and judge.py)
# share ONE embedder singleton and ONE fail-open rate-limiter, so they don't each
# spin up a provider or independently flood the table during an embedding outage.
_EMBEDDING_PROVIDER: EmbeddingProvider | None = None

# Fail-open rate limiter: when the embedder is unavailable the novelty gate can't
# check, so it defaults to "novel". This per-task_type cooldown caps stores to one
# per window to prevent table flooding during extended outages. Keyed by task_type
# → time.monotonic() of the last fail-open store. SHARED across both paths.
FAIL_OPEN_COOLDOWN_SECS = 3600  # 1 hour
_fail_open_timestamps: dict[str, float] = {}


def get_embedding_provider() -> EmbeddingProvider | None:
    """Lazy shared singleton embedder for the procedure novelty gate. Returns
    None if no embedding backend is configured (callers fall open and store
    without novelty filtering, rate-limited via _fail_open_timestamps).
    """
    global _EMBEDDING_PROVIDER
    if _EMBEDDING_PROVIDER is None:
        try:
            from genesis.memory.embeddings import EmbeddingProvider

            _EMBEDDING_PROVIDER = EmbeddingProvider()
        except Exception:
            logger.warning(
                "EmbeddingProvider unavailable; procedure novelty gate disabled",
                exc_info=True,
            )
            return None
    return _EMBEDDING_PROVIDER


def pack_embedding(vector: list[float]) -> bytes:
    """Pack a float vector into a little-endian float32 BLOB.

    Raises ValueError if the vector dimensionality is unexpected — store
    sites should treat that as "skip embedding" rather than corrupting the
    column.
    """
    if len(vector) != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding dimension mismatch: got {len(vector)}, expected {EMBEDDING_DIM}"
        )
    return struct.pack(f"<{EMBEDDING_DIM}f", *vector)


def unpack_embedding(blob: bytes | None) -> list[float] | None:
    """Inverse of pack_embedding. Returns None on any failure (bad length,
    corrupted bytes, NULL) so the hook can skip the row instead of crashing.
    """
    if not blob or len(blob) != EMBEDDING_DIM * 4:
        return None
    try:
        return list(struct.unpack(f"<{EMBEDDING_DIM}f", blob))
    except struct.error:
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. Returns 0.0 on any
    edge case (length mismatch, zero norm) so callers can treat the value
    as a relevance score without separate error handling.
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize each row of an ``(N, D)`` matrix.

    Zero-norm rows are left as zero rows, so their dot with any query is 0.0 —
    matching :func:`cosine_similarity`'s zero-norm contract. Intended to run
    ONCE at cache-build time (see ``memory.proactive._load_procedure_cache``),
    hoisting normalization out of the per-query hot path.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def cosine_similarity_batch(normalized_matrix: np.ndarray, query: list[float]) -> np.ndarray:
    """Cosine of ``query`` against every row of a PRE-normalized ``(N, D)`` matrix.

    Returns an ``(N,)`` float64 array — the vectorized (single matmul, GIL-
    releasing) equivalent of calling :func:`cosine_similarity` once per row,
    without the per-row Python overhead. Rows MUST already be L2-normalized via
    :func:`normalize_rows`. Edge cases return an all-zeros result to match the
    scalar contract: empty matrix, zero-norm query, or a query whose length
    differs from the matrix width (the scalar helper returns 0.0 on length
    mismatch, never raising).
    """
    n = normalized_matrix.shape[0]
    if normalized_matrix.size == 0:
        return np.zeros(n, dtype=np.float64)
    q = np.asarray(query, dtype=np.float64)
    if q.ndim != 1 or q.shape[0] != normalized_matrix.shape[1]:
        return np.zeros(n, dtype=np.float64)
    qn = np.linalg.norm(q)
    if qn == 0.0:
        return np.zeros(n, dtype=np.float64)
    return normalized_matrix @ (q / qn)
