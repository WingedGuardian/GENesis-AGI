"""Unit tests for the shared FTS5 OR-fallback helper (db/crud/_fts.py).

FTS5's default MATCH is implicit-AND, so a multi-word query starves unless every
token is present. ``fetch_fts`` keeps AND-first precision but retries OR-joined
when AND finds nothing — only ADDING results, never changing a query that hit.
"""

import sqlite3

import pytest

from genesis.db.crud._fts import fetch_fts, or_fallback

# FTS5 may be unavailable in the in-memory SQLite build.
_fts5_available = True
try:
    _c = sqlite3.connect(":memory:")
    _c.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
    _c.close()
except Exception:
    _fts5_available = False


def test_or_fallback_multi_term_joins_with_or():
    assert or_fallback("alpha beta gamma") == "alpha OR beta OR gamma"


def test_or_fallback_single_term_is_none():
    # single term: AND == OR, no fallback needed
    assert or_fallback("solo") is None


def test_or_fallback_empty_is_none():
    assert or_fallback("") is None
    assert or_fallback("   ") is None


def test_or_fallback_drops_stopwords():
    # A verbose query OR-joins only the MEANINGFUL terms, not the/is/where noise.
    assert or_fallback("where is the nonexistent service") == "nonexistent OR service"


def test_or_fallback_all_stopwords_kept():
    # If every token is a stopword, keep them all — an OR still beats nothing.
    assert or_fallback("the a of") == "the OR a OR of"


def test_or_fallback_reduces_to_single_meaningful_term():
    # Multi-term query reducing to ONE meaningful term -> that term alone
    # (a valid single-term FTS query, strictly better than the zero-row AND).
    assert or_fallback("where is the service") == "service"


@pytest.mark.skipif(not _fts5_available, reason="FTS5 not available")
class TestFetchFts:
    @pytest.fixture
    async def db(self):
        import aiosqlite

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("CREATE VIRTUAL TABLE t USING fts5(body, tokenize='porter ascii')")
        await conn.execute("INSERT INTO t(body) VALUES ('alpha beta gamma')")
        await conn.commit()
        yield conn
        await conn.close()

    _SQL = "SELECT body FROM t WHERE t MATCH ?"

    async def test_and_miss_falls_back_to_or(self, db):
        # 'alpha zzz' as implicit-AND requires BOTH -> 0 rows; OR-fallback finds 'alpha'.
        rows = await fetch_fts(db, self._SQL, ["alpha zzz"])
        assert rows and rows[0][0] == "alpha beta gamma"

    async def test_and_hit_needs_no_fallback(self, db):
        # both terms present -> AND already returns; fallback irrelevant.
        rows = await fetch_fts(db, self._SQL, ["alpha beta"])
        assert rows and rows[0][0] == "alpha beta gamma"

    async def test_boolean_true_skips_fallback(self, db):
        # boolean=True (already-structured query): no re-tokenising, AND miss stays empty.
        rows = await fetch_fts(db, self._SQL, ["alpha zzz"], boolean=True)
        assert rows == []

    async def test_single_absent_term_stays_empty(self, db):
        # single term, genuinely absent: no fallback can help, stays empty.
        rows = await fetch_fts(db, self._SQL, ["zzz"])
        assert rows == []
