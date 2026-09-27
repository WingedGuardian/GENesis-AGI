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
        # id -> (principle, tier) as the table holds it NOW (post-snapshot edits)
        self._live_overrides: dict[str, tuple] = {}

    async def execute_fetchall(self, _sql: str, params: object = None) -> list:
        if params is not None:  # winner recheck -> LIVE (principle, tier)
            if params[0] not in self._live_ids:
                return []
            row = next(r for r in self._rows if r[0] == params[0])
            live = self._live_overrides.get(params[0], (row[2], row[4], row[3]))
            return [live]
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
    proactive._procedure_refresh_in_flight = False
    yield
    proactive._procedure_cache = None
    proactive._procedure_refresh_in_flight = False


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


# --------------------------------------------------------------------------- #
# Stale-while-revalidate: past the TTL the request path SERVES the snapshot and
# schedules one background rebuild; it never rebuilds inline. (Recall calls on a
# single-user install arrive minutes apart, so refresh-on-read made almost every
# call pay the 260–570ms rebuild.)
# --------------------------------------------------------------------------- #


class _CountingDB(_FakeDB):
    def __init__(self, rows: list[_Row]) -> None:
        super().__init__(rows)
        self.bulk_reads = 0

    async def execute_fetchall(self, _sql: str, params: object = None) -> list:
        if params is None:
            self.bulk_reads += 1
        return await super().execute_fetchall(_sql, params)


async def test_first_build_is_inline() -> None:
    rng = np.random.default_rng(3)
    db = _CountingDB([("a", "t", "p", pack_embedding(_rand_vec(rng)), "CORE")])
    cache = await proactive._load_procedure_cache(db)
    assert cache is not None and [m[0] for m in cache.meta] == ["a"]
    assert db.bulk_reads == 1


async def test_expired_cache_is_served_and_refreshed_in_background() -> None:
    import asyncio

    rng = np.random.default_rng(4)
    old = await proactive._load_procedure_cache(
        _FakeDB([("old", "t", "p", pack_embedding(_rand_vec(rng)), "CORE")])
    )
    assert old is not None
    # Age the snapshot past the TTL.
    proactive._procedure_cache = proactive._ProcedureCache(
        matrix=old.matrix,
        meta=old.meta,
        built_at=old.built_at - proactive._PROCEDURE_CACHE_TTL_S - 1,
    )
    db = _CountingDB([("new", "t", "p", pack_embedding(_rand_vec(rng)), "CORE")])

    served = await proactive._load_procedure_cache(db)
    # The request path got the STALE snapshot, with no inline read.
    assert [m[0] for m in served.meta] == ["old"]
    assert db.bulk_reads == 0
    assert proactive._procedure_refresh_in_flight is True
    # A second expired read while the refresh is in flight schedules nothing new.
    await proactive._load_procedure_cache(db)

    for _ in range(50):
        if not proactive._procedure_refresh_in_flight:
            break
        await asyncio.sleep(0.01)
    assert proactive._procedure_refresh_in_flight is False
    assert db.bulk_reads == 1  # single-flight
    assert [m[0] for m in proactive._procedure_cache.meta] == ["new"]


async def test_background_refresh_failure_keeps_the_prior_snapshot() -> None:
    import asyncio

    rng = np.random.default_rng(5)
    old = await proactive._load_procedure_cache(
        _FakeDB([("old", "t", "p", pack_embedding(_rand_vec(rng)), "CORE")])
    )
    proactive._procedure_cache = proactive._ProcedureCache(
        matrix=old.matrix,
        meta=old.meta,
        built_at=old.built_at - proactive._PROCEDURE_CACHE_TTL_S - 1,
    )

    class _Broken:
        async def execute_fetchall(self, *_a, **_k):
            raise RuntimeError("db down")

    await proactive._load_procedure_cache(_Broken())
    for _ in range(50):
        if not proactive._procedure_refresh_in_flight:
            break
        await asyncio.sleep(0.01)
    assert proactive._procedure_refresh_in_flight is False  # flag never wedges
    assert [m[0] for m in proactive._procedure_cache.meta] == ["old"]


async def test_surfaces_the_live_principle_and_tier_not_the_snapshot() -> None:
    """PR #2455 review: under SWR the snapshot can outlive the TTL, so the
    surfaced text/tier must come from the live winner row, and a winner demoted
    to DORMANT since the snapshot must clear the stricter DORMANT bar."""
    rng = np.random.default_rng(21)
    q = _rand_vec(rng)
    row = ("w1", "task", "old advice", pack_embedding(q), "CORE")
    db = _FakeDB([row])
    await proactive._load_procedure_cache(db)  # snapshot says "old advice"/CORE

    db._live_overrides["w1"] = ("new advice", "CORE", row[3])
    got = await proactive._surface_procedure(db, q)
    assert got is not None and got["principle"] == "new advice"

    # Demoted since the snapshot, and the match (~0.74) is below DORMANT's 0.78.
    other = [float(x) for x in rng.standard_normal(EMBEDDING_DIM)]
    mix = [0.74 * a + (1 - 0.74**2) ** 0.5 * b for a, b in zip(q, _unit(other, q), strict=True)]
    assert (await proactive._surface_procedure(db, mix)) is not None  # CORE bar 0.70: clears
    db._live_overrides["w1"] = ("new advice", "DORMANT", row[3])
    assert await proactive._surface_procedure(db, mix) is None


def _unit(v: list[float], against: list[float]) -> list[float]:
    """Unit vector orthogonal to ``against`` (Gram-Schmidt), for a known cosine."""
    a = np.asarray(against)
    a = a / np.linalg.norm(a)
    x = np.asarray(v)
    x = x - x.dot(a) * a
    return list(x / np.linalg.norm(x) * np.linalg.norm(np.asarray(against)))


async def test_refined_procedure_is_rescored_against_its_live_embedding() -> None:
    """PR #2455 round 2: a refine updates principle AND embedding together. The
    winner must be re-scored against the LIVE embedding, so revised advice only
    surfaces if it clears the bar on its own vector — not on the old one's."""
    rng = np.random.default_rng(31)
    q = _rand_vec(rng)
    row = ("w2", "task", "old advice", pack_embedding(q), "CORE")
    db = _FakeDB([row])
    await proactive._load_procedure_cache(db)

    unrelated = _unit([float(x) for x in rng.standard_normal(EMBEDDING_DIM)], q)
    db._live_overrides["w2"] = ("rewritten advice", "CORE", pack_embedding(unrelated))
    assert await proactive._surface_procedure(db, q) is None

    # Control: a refine whose new vector still matches surfaces the new text.
    db._live_overrides["w2"] = ("rewritten advice", "CORE", pack_embedding(q))
    got = await proactive._surface_procedure(db, q)
    assert got is not None and got["principle"] == "rewritten advice"
