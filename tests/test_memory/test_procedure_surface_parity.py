"""Parity guard for the vectorized procedure surfacing (A1).

``memory.proactive._surface_procedure`` was reworked from a per-row pure-Python
``cosine_similarity`` loop over ~300 principle embeddings (read + unpacked every
call) into one cached matmul against a TTL-cached, pre-normalized matrix. These
tests lock the vectorized path to the exact behavior of the old scalar loop —
same cosines (within float tolerance), same tie-break, same per-tier thresholds.
"""

from __future__ import annotations

import numpy as np
import pytest

from genesis.learning.procedural.embedding import (
    EMBEDDING_DIM,
    cosine_similarity,
    cosine_similarity_batch,
    normalize_rows,
    pack_embedding,
)
from genesis.memory import proactive


def _rand_vec(rng: np.random.Generator, dim: int = EMBEDDING_DIM) -> list[float]:
    return [float(x) for x in rng.standard_normal(dim)]


# --------------------------------------------------------------------------- #
# Helper-level parity: batched cosine == scalar cosine, per row.
# --------------------------------------------------------------------------- #


def test_cosine_batch_matches_scalar_randomized() -> None:
    rng = np.random.default_rng(1234)
    for _ in range(100):
        n = int(rng.integers(1, 40))
        dim = int(rng.integers(2, 64))
        vecs = [_rand_vec(rng, dim) for _ in range(n)]
        if n > 2:  # ensure a zero row is exercised (cosine 0.0)
            vecs[1] = [0.0] * dim
        query = _rand_vec(rng, dim)

        matrix = normalize_rows(np.asarray(vecs, dtype=np.float64))
        batched = cosine_similarity_batch(matrix, query)
        scalar = [cosine_similarity(query, v) for v in vecs]

        assert np.allclose(batched, scalar, atol=1e-9), (batched.tolist(), scalar)


def test_cosine_batch_edge_cases() -> None:
    matrix = normalize_rows(np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float64))
    # zero-norm query -> all zeros (matches scalar contract)
    assert list(cosine_similarity_batch(matrix, [0.0, 0.0])) == [0.0, 0.0]
    # length mismatch -> all zeros, never raises (scalar returns 0.0)
    assert list(cosine_similarity_batch(matrix, [1.0, 0.0, 0.0])) == [0.0, 0.0]
    # empty matrix -> empty result
    assert cosine_similarity_batch(np.empty((0, 0)), [1.0, 0.0]).shape == (0,)


# --------------------------------------------------------------------------- #
# Function-level parity: _surface_procedure vs a verbatim reference of the
# pre-refactor scalar algorithm.
# --------------------------------------------------------------------------- #

_Row = tuple[str, str, str, bytes, str | None]


class _FakeDB:
    """Stand-in for the runtime db. The bulk query (no params) returns the cached
    rows; the winner recheck (``WHERE id = ?``, one param) returns a truthy row
    iff that id is still "live" (default: every row is live)."""

    def __init__(self, rows: list[_Row], live_ids: set[str] | None = None) -> None:
        self._rows = rows
        self._live_ids = {r[0] for r in rows} if live_ids is None else set(live_ids)

    async def execute_fetchall(self, _sql: str, params: object = None) -> list:
        if params is not None:  # winner recheck
            return [(1,)] if params[0] in self._live_ids else []
        return self._rows


def _reference_surface(rows: list[_Row], vector: list[float]) -> dict | None:
    """The old per-row scalar algorithm, verbatim (the oracle)."""
    best: tuple[float, str, str, str, str] | None = None
    for row in rows:
        from genesis.learning.procedural.embedding import unpack_embedding

        existing = unpack_embedding(row[3])
        if existing is None:
            continue
        sim = cosine_similarity(vector, existing)
        tier = row[4] or "DORMANT"
        threshold = 0.78 if tier == "DORMANT" else 0.7
        if sim < threshold:
            continue
        if best is None or sim > best[0]:
            best = (sim, row[0], row[1] or "", row[2] or "", tier)
    if best is None:
        return None
    _sim, proc_id, task_type, principle, tier = best
    return {"id": proc_id, "task_type": task_type, "principle": principle[:200], "tier": tier}


@pytest.fixture(autouse=True)
def _reset_procedure_cache():
    """The TTL cache is module-global — clear it around every case so each fake
    db is actually read rather than served stale."""
    proactive._procedure_cache = None
    yield
    proactive._procedure_cache = None


async def test_surface_procedure_matches_reference_randomized() -> None:
    rng = np.random.default_rng(42)
    tiers = ["CORE", "ADVISORY", "LIBRARY", "DORMANT", None]
    for case in range(120):
        query = np.asarray(_rand_vec(rng), dtype=np.float64)
        rows: list[_Row] = []
        n = int(rng.integers(0, 12))
        for i in range(n):
            # Blend the query direction with noise so cosines span both sides of
            # the 0.7 / 0.78 tier bars (some surface, some don't).
            w = float(rng.uniform(0.4, 1.0))
            emb = w * query + (1.0 - w) * np.asarray(_rand_vec(rng), dtype=np.float64)
            tier = tiers[int(rng.integers(0, len(tiers)))]
            rows.append(
                (
                    f"proc{i:02d}",
                    f"task_type_{i}",
                    f"principle text {i} " + "x" * 250,  # > 200 chars → exercises [:200]
                    pack_embedding([float(x) for x in emb]),
                    tier,
                )
            )

        proactive._procedure_cache = None
        got = await proactive._surface_procedure(_FakeDB(rows), [float(x) for x in query])
        want = _reference_surface(rows, [float(x) for x in query])
        assert got == want, (case, got, want)


async def test_surface_procedure_empty_and_bad_rows() -> None:
    # No rows -> None
    assert (
        await proactive._surface_procedure(_FakeDB([]), _rand_vec(np.random.default_rng(1))) is None
    )

    # A row with a corrupt (wrong-length) embedding blob is skipped, not crashed.
    rng = np.random.default_rng(7)
    q = _rand_vec(rng)
    good = (
        "good",
        "task",
        "p",
        pack_embedding(q),  # identical to query → cosine 1.0, clears any bar
        "CORE",
    )
    bad = ("bad", "task", "p", b"\x00\x01\x02", "CORE")  # wrong length → unpack None
    proactive._procedure_cache = None
    got = await proactive._surface_procedure(_FakeDB([bad, good]), q)
    assert got is not None and got["id"] == "good"


async def test_surface_procedure_rechecks_live_winner() -> None:
    """A procedure still in the (≤TTL stale) cache but quarantined/deprecated
    since the build must NOT surface — the winner is re-verified live first."""
    rng = np.random.default_rng(11)
    q = _rand_vec(rng)
    row = ("q1", "task", "p", pack_embedding(q), "CORE")  # self-match clears the bar

    proactive._procedure_cache = None
    got = await proactive._surface_procedure(_FakeDB([row]), q)
    assert got is not None and got["id"] == "q1"  # live → surfaces

    proactive._procedure_cache = None
    got = await proactive._surface_procedure(_FakeDB([row], live_ids=set()), q)
    assert got is None  # excluded since build → suppressed
