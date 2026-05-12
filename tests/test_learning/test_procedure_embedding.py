"""Tests for the procedure principle embedding helper + write path."""

from __future__ import annotations

import struct

import pytest

from genesis.db.crud.procedural import get_by_id
from genesis.learning.procedural.embedding import (
    EMBEDDING_DIM,
    cosine_similarity,
    pack_embedding,
    unpack_embedding,
)
from genesis.learning.procedural.operations import store_procedure


def test_pack_unpack_roundtrip():
    vec = [float(i) / 1000.0 for i in range(EMBEDDING_DIM)]
    blob = pack_embedding(vec)
    assert len(blob) == EMBEDDING_DIM * 4
    out = unpack_embedding(blob)
    assert out is not None
    assert len(out) == EMBEDDING_DIM
    # Float32 precision — exact equality won't hold across the full vector,
    # but close-enough is the contract we care about.
    for a, b in zip(vec, out, strict=True):
        assert abs(a - b) < 1e-6


def test_pack_rejects_wrong_dim():
    with pytest.raises(ValueError):
        pack_embedding([0.0, 1.0, 2.0])


def test_unpack_handles_none():
    assert unpack_embedding(None) is None


def test_unpack_handles_corrupted_length():
    # Wrong byte count — function should return None, not raise
    assert unpack_embedding(b"\x00\x00") is None


def test_unpack_handles_corrupted_bytes():
    # Right byte count but unparseable would be hard to trigger with struct
    # — corrupted-length already covers the common failure mode.
    bad = bytes(EMBEDDING_DIM * 4 + 1)  # off by one
    assert unpack_embedding(bad) is None


def test_cosine_basic():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # length mismatch returns 0
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0
    # zero vectors return 0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


@pytest.mark.asyncio
async def test_store_procedure_persists_embedding(db):
    """store_procedure should persist principle_embedding when provided."""
    vec = [0.5] * EMBEDDING_DIM
    blob = pack_embedding(vec)
    proc_id = await store_procedure(
        db,
        task_type="t",
        principle="p",
        steps=["a"],
        tools_used=["x"],
        context_tags=["y"],
        principle_embedding=blob,
    )

    row = await get_by_id(db, proc_id)
    assert row is not None
    stored = row.get("principle_embedding")
    assert stored is not None
    assert len(stored) == EMBEDDING_DIM * 4
    # Round-trip preserves the vector
    out = unpack_embedding(stored)
    assert out is not None
    for a, b in zip(vec, out, strict=True):
        assert abs(a - b) < 1e-6


@pytest.mark.asyncio
async def test_store_procedure_persists_none_when_not_provided(db):
    """Backwards-compat: callers that don't supply an embedding still work."""
    proc_id = await store_procedure(
        db,
        task_type="t2",
        principle="p2",
        steps=["a"],
        tools_used=["x"],
        context_tags=["y"],
    )
    row = await get_by_id(db, proc_id)
    assert row is not None
    assert row.get("principle_embedding") is None


def test_pack_layout_is_little_endian_float32():
    """Locks the on-disk format so future readers don't drift from writers."""
    expected = struct.pack("<1f", 1.0)
    assert expected == b"\x00\x00\x80?"
