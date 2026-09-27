"""bm25-inert term pruning for top-level FTS5 disjunctions (``_fts.drop_bm25_inert_terms``).

The property that makes pruning safe is FTS5's own bm25: a phrase in at least
half the rows gets its IDF clamped to ~1e-6, so OR-ing it in only adds ~0-scored
rows while forcing FTS5 to score them all. These tests pin (1) the df >= N/2
classification, measured by the engine itself; (2) that dropping inert terms
from a top-level OR leaves the top-K candidate SET unchanged on a corpus where
the rest of the query has enough hits; (3) the fail-open contract.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import _fts
from genesis.db.crud._fts import drop_bm25_inert_terms


@pytest.fixture(autouse=True)
def _clean_cache():
    _fts._reset_df_cache()
    yield
    _fts._reset_df_cache()


async def _corpus(n: int = 200) -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "memory_id UNINDEXED, content, source_type, tags, collection UNINDEXED, "
        'tokenize="porter ascii")'
    )
    rows = []
    for i in range(n):
        words = ["common"]  # every row: the structural-tag shape (df = N)
        if i % 2 == 0:
            words.append("halfword")  # exactly N/2 rows -> inert (boundary)
        if i % 5 == 0:
            words.append("rareword")  # 20% -> NOT inert
        if i % 50 == 0:
            words.append("needle")  # 2% -> NOT inert
        rows.append((f"m{i}", " ".join(words), "memory", "wing:memory", "episodic_memory"))
    await db.executemany("INSERT INTO memory_fts VALUES (?, ?, ?, ?, ?)", rows)
    await db.commit()
    return db


async def test_drops_terms_at_or_above_half_the_corpus():
    db = await _corpus()
    try:
        kept = await drop_bm25_inert_terms(db, ["common", "halfword", "rareword", "needle"])
        # "memory" is a tag in every row -> inert too; stemming is FTS5's own.
        kept_tag = await drop_bm25_inert_terms(db, ["memories", "needle"])
    finally:
        await db.close()
    assert kept == ["rareword", "needle"]
    assert kept_tag == ["needle"]


async def test_all_inert_returns_empty():
    db = await _corpus()
    try:
        assert await drop_bm25_inert_terms(db, ["common", "halfword"]) == []
    finally:
        await db.close()


async def test_pruned_disjunction_keeps_the_top_k_candidate_set():
    """The rank-neutrality claim, checked against the real engine: the lane the
    production code prunes is ``(base) OR (file terms)``; with the inert file
    term removed the top-K SET must be identical (the dropped rows scored ~0)."""
    db = await _corpus(400)
    sql = "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 10"
    try:
        files = ["common", "rareword"]
        kept = await drop_bm25_inert_terms(db, files)
        assert kept == ["rareword"]
        before = [
            r[0] for r in await db.execute_fetchall(sql, (f"(needle) OR ({' OR '.join(files)})",))
        ]
        after = [
            r[0] for r in await db.execute_fetchall(sql, (f"(needle) OR ({' OR '.join(kept)})",))
        ]
    finally:
        await db.close()
    assert len(before) == 10
    assert set(before) == set(after)


async def test_fails_open_when_the_table_is_missing():
    db = await aiosqlite.connect(":memory:")
    try:
        assert await drop_bm25_inert_terms(db, ["a", "b"]) == ["a", "b"]
    finally:
        await db.close()


async def test_df_is_cached_across_calls():
    db = await _corpus()
    calls = 0
    real = db.execute_fetchall

    async def counting(sql, params=()):
        nonlocal calls
        if "MATCH" in sql:
            calls += 1
        return await real(sql, params)

    db.execute_fetchall = counting  # type: ignore[method-assign]
    try:
        await drop_bm25_inert_terms(db, ["common", "needle"])
        first = calls
        await drop_bm25_inert_terms(db, ["common", "needle"])
    finally:
        await db.close()
    assert first == 2
    assert calls == first  # second call served from the cache


async def test_empty_input_is_a_no_op():
    assert await drop_bm25_inert_terms(None, []) == []  # type: ignore[arg-type]


async def test_file_keyword_lane_is_pruned_in_the_real_composer():
    """Wiring: HybridRetriever._expand_fts_query routes the file-keyword lane
    through ``_ro_read(drop_bm25_inert_terms, …)`` — the inert term is gone from
    the composed expression and a selective one survives."""
    from genesis.memory.retrieval import HybridRetriever

    db = await _corpus()

    class _Reads:
        async def _ro_read(self, fn, *args, **kwargs):
            return await fn(db, *args, **kwargs)

    try:
        composed = await HybridRetriever._expand_fts_query(
            _Reads(),
            query="graph expansion",
            collections=[],
            expand_query_terms=False,
            extra_fts_terms=["common", "needle"],
        )
        all_inert = await HybridRetriever._expand_fts_query(
            _Reads(),
            query="graph expansion",
            collections=[],
            expand_query_terms=False,
            extra_fts_terms=["common", "halfword"],
        )
    finally:
        await db.close()
    assert composed == "(graph expansion) OR (needle)"
    # Every file term inert -> the whole lane is dropped, query left untouched.
    assert all_inert == "graph expansion"
