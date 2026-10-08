"""Many-term FTS5 MATCH expressions built from free text are bounded.

A long pasted prompt used to become an OR (or AND) of every one of its words,
which made FTS5 score most of the corpus: slow enough to time out memory recall,
and large enough to spill a 60-190 MiB temp sort. ``bounded_terms`` caps the
distinct terms at ``FTS_MAX_TERMS`` at every producer that builds such an
expression, and leaves every query that fits the budget exactly as it was.
"""

from __future__ import annotations

import logging

import aiosqlite
import pytest

from genesis.db.crud import _fts
from genesis.db.crud._fts import FTS_MAX_TERMS, bounded_terms, or_fallback
from genesis.db.crud.memory import _prepare_fts5

N = FTS_MAX_TERMS


def _long_prompt(distinct: int = 2000) -> str:
    # Distinct words plus a few repeated ones, the shape of a pasted log.
    words = [f"tok{i}" for i in range(distinct)]
    words += ["retrieval"] * 7 + ["recall"] * 5 + ["spill"] * 3
    return " ".join(words)


def _distinct_terms(expression: str) -> set[str]:
    return {
        t for t in expression.replace("(", " ").replace(")", " ").split() if t not in {"AND", "OR"}
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    _fts._reset_df_cache()
    yield
    _fts._reset_df_cache()


# ── bounded_terms ─────────────────────────────────────────────────────────────


def test_at_or_under_the_cap_the_input_comes_back_untouched():
    terms = ["alpha", "beta", "alpha", "gamma"]
    assert bounded_terms(terms, 3) is terms  # same object: dups and order kept


def test_over_the_cap_keeps_the_most_frequent_ties_by_first_appearance():
    terms = ["a", "b", "c", "b", "d", "c", "b", "e"]
    # counts: b=3, c=2, then a, d, e at 1 (a appeared first)
    assert bounded_terms(terms, 3) == ["a", "b", "c"]  # returned in first-appearance order


def test_over_the_cap_de_duplicates():
    kept = bounded_terms(["x"] * 5 + [f"t{i}" for i in range(10)], 4)
    assert len(kept) == len(set(kept)) == 4
    assert "x" in kept


def test_the_cap_logs_once_and_only_when_it_applies(caplog):
    caplog.set_level(logging.INFO, logger="genesis.db.crud._fts")
    bounded_terms(["a", "b"], 5, site="t")
    assert not caplog.records
    bounded_terms([f"t{i}" for i in range(10)], 5, site="unit")
    assert len(caplog.records) == 1
    assert "10 distinct -> 5" in caplog.text and "site=unit" in caplog.text


# ── site C: the OR retry ──────────────────────────────────────────────────────


def test_or_retry_of_a_long_query_is_bounded():
    alt = or_fallback(_prepare_fts5(_long_prompt()))
    terms = alt.split(" OR ")
    assert len(terms) == len(set(terms)) == N
    # the repeated words outrank the one-off ones
    assert {"retrieval", "recall", "spill"} <= set(terms)


def test_or_retry_of_a_short_query_is_unchanged():
    assert or_fallback("where is the nonexistent service") == "nonexistent OR service"


# ── site A: the expanded query ────────────────────────────────────────────────


class _NoQdrant:
    def get_collection(self, name):
        raise RuntimeError("no qdrant in tests")


@pytest.fixture
def _expansion(monkeypatch):
    from genesis.memory import intent

    monkeypatch.setattr(intent, "_last_count_check", 0.0)
    monkeypatch.setattr(
        intent._tag_index, "expand", lambda keywords, max_expansions=5: ["boostterm"]
    )
    return intent


async def test_expanded_query_of_a_long_prompt_is_bounded(_expansion):
    expanded = await _expansion.expand_query(_long_prompt(), _NoQdrant(), ["c"])
    keywords = _distinct_terms(expanded) - {"boostterm"}
    assert len(keywords) == N
    assert {"retrieval", "recall", "spill"} <= keywords


async def test_expanded_query_of_a_short_prompt_is_unchanged(_expansion):
    expanded = await _expansion.expand_query("how does recall retrieval work", _NoQdrant(), ["c"])
    assert (
        expanded
        == "(recall AND retrieval AND work) OR ((recall OR retrieval OR work) AND (boostterm))"
    )


async def test_tokenize_query_itself_is_not_bounded():
    # classify_stance reads _tokenize_query's order and length; the cap must not.
    from genesis.memory.intent import _tokenize_query

    assert len(_tokenize_query(_long_prompt())) > N


# ── site B: the raw prompt as the file-keyword lane's base ────────────────────


async def _corpus() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "memory_id UNINDEXED, content, source_type, tags, collection UNINDEXED, "
        'tokenize="porter ascii")'
    )
    rows = [
        (f"m{i}", f"common needle{i % 3}", "memory", "wing:memory", "episodic_memory")
        for i in range(60)
    ]
    await db.executemany("INSERT INTO memory_fts VALUES (?, ?, ?, ?, ?)", rows)
    await db.commit()
    return db


class _Reads:
    def __init__(self, db):
        self._db = db

    async def _ro_read(self, fn, *args, **kwargs):
        return await fn(self._db, *args, **kwargs)


async def _compose(query: str, *, extra: list[str] | None) -> tuple[str, str | None]:
    from genesis.memory.retrieval import HybridRetriever

    db = await _corpus()
    try:
        return await HybridRetriever._expand_fts_query(
            _Reads(db),
            query=query,
            collections=[],
            expand_query_terms=False,
            extra_fts_terms=extra,
        )
    finally:
        await db.close()


async def test_lane_base_of_a_long_prompt_is_bounded():
    composed, _ = await _compose(_long_prompt(), extra=["needle1"])
    base = composed.split(") OR (")[0].lstrip("(")
    assert len(_distinct_terms(base)) == N


async def test_lane_base_of_a_short_prompt_is_unchanged():
    composed, _ = await _compose("Graph Expansion", extra=["needle1"])
    assert composed == "(Graph Expansion) OR (needle1)"


async def test_plain_path_is_left_to_the_or_retry():
    # No lane and no expansion: the query must come back as the SAME string, so
    # the caller's `fts_query != query` boolean flag stays False and the AND pass
    # plus the (bounded) OR retry still run. Bounding here would silently switch
    # every long prompt to strict AND with no fallback.
    long = _long_prompt()
    composed, fallback = await _compose(long, extra=None)
    assert composed is long or composed == long
    assert fallback is None


# ── every composed expression is still valid FTS5 ─────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        _long_prompt(),
        # Operators and stray parentheses in a LONG prompt: the bounded base is
        # lowercased \w tokens, so it parses.
        _long_prompt() + " AND OR NOT ( NEAR( x",
        # A SHORT prompt keeps its exact bytes as the file-lane base, so its
        # operators and unbalanced parentheses still reach boolean FTS5. That is
        # pre-existing (search_ranked then retries as bare terms and drops the
        # lane); a long prompt is safe because bounding lowercases its base.
        pytest.param(
            "AND OR NOT ( ) NEAR( x",
            marks=pytest.mark.xfail(
                strict=True, reason="pre-existing: raw short lane base is not operator-safe"
            ),
        ),
        "Graph Expansion",
    ],
)
async def test_composed_expressions_parse_in_fts5(prompt, monkeypatch):
    from genesis.memory import intent

    monkeypatch.setattr(intent, "_last_count_check", 0.0)
    monkeypatch.setattr(
        intent._tag_index, "expand", lambda keywords, max_expansions=5: ["boostterm"]
    )
    expanded = await intent.expand_query(prompt, _NoQdrant(), ["c"])
    lane, _ = await _compose(prompt, extra=["needle1"])
    db = await _corpus()
    try:
        for expression, boolean in ((expanded, expanded != prompt), (lane, True)):
            match = _prepare_fts5(expression, boolean=boolean)
            if match:
                await db.execute_fetchall(
                    "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? LIMIT 1", [match]
                )
    finally:
        await db.close()
