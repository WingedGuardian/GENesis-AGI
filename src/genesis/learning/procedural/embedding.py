"""Procedure principle embedding — pack/unpack + cosine helpers.

Procedures store `principle_embedding` as a BLOB of 1024 little-endian
float32 values (4096 bytes). Storing the embedding avoids re-embedding
existing principles on every proactive-hook firing.

The hook reads the BLOB, unpacks to a vector, computes cosine vs the
already-embedded prompt vector, and surfaces a procedure when the max
cosine across all procedures crosses the activation threshold.
"""

from __future__ import annotations

import math
import struct

# qwen3-embedding output dimensionality. Matches genesis.memory.embeddings.
EMBEDDING_DIM = 1024


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
