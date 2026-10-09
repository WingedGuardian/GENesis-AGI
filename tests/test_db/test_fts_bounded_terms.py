"""Many-term FTS5 MATCH expressions built from free text are bounded.

A long pasted prompt used to become an OR (or AND) of every one of its words,
which made FTS5 score most of the corpus: slow enough to time out memory recall,
and large enough to spill a 60-190 MiB temp sort. ``bounded_terms`` caps the
distinct terms at ``FTS_MAX_TERMS`` at every producer that builds such an
expression, and leaves every query that fits the budget exactly as it was. The
budget is FTS5 tokens over every operand: repeats count, and a snake_case word
counts once per piece, because that is what FTS5 evaluates.
"""

from __future__ import annotations

import logging

import aiosqlite
import pytest

from genesis.db.crud import _fts
from genesis.db.crud._fts import (
    FTS_MAX_TERMS,
    and_pass,
    bounded_terms,
    fts5_query_tokens,
    fts5_tokens,
    or_fallback,
)
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
    assert bounded_terms(terms, 4) is terms  # same object: dups and order kept
    # 4 operands over a budget of 3: the repeat counts, so the cap applies.
    assert bounded_terms(terms, 3) == ["alpha", "beta", "gamma"]


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
    assert "10 operands (10 distinct, 10 tokens) -> 5" in caplog.text
    assert "site=unit" in caplog.text


# ── the budget counts what FTS5 evaluates ─────────────────────────────────────


def test_repeated_operands_count_toward_the_budget():
    # A pasted log repeating one line: few distinct words, many operands.
    terms = ["spam", "eggs"] * 500
    assert bounded_terms(terms, 32) == ["spam", "eggs"]


def test_a_query_whose_operands_fit_is_untouched_even_with_repeats():
    terms = ["spam", "eggs"] * 16  # 32 operands, 32 tokens
    assert bounded_terms(terms, 32) is terms


def test_or_retry_of_a_long_low_vocabulary_paste_is_bounded():
    alt = or_fallback(_prepare_fts5(" ".join(["disk spill retrieval"] * 400)))
    assert alt == "disk OR spill OR retrieval"


async def test_lane_base_of_a_long_low_vocabulary_paste_is_bounded():
    composed, _ = await _compose(" ".join(["disk spill retrieval"] * 400), extra=["needle1"])
    assert composed == "(disk spill retrieval) OR (needle1)"


def test_snake_case_terms_count_one_token_per_piece():
    ids = [f"alpha_beta_{i}" for i in range(20)]  # 3 tokens each, 60 in all
    kept = bounded_terms(ids, 32)
    assert sum(fts5_tokens(t) for t in kept) <= 32
    assert kept == ids[:10]  # all tie at count 1: first appearance fills 30 of 32


def test_a_term_longer_than_the_whole_budget_is_cut_not_dropped():
    long_id = "_".join(f"p{i}" for i in range(40))
    assert bounded_terms([long_id], 32) == ["_".join(f"p{i}" for i in range(32))]


@pytest.mark.parametrize(
    "term", ["plain", "snake_case_word", "a__b", "_lead", "trail_", "x1_2y", "naïve_café"]
)
async def test_fts5_tokens_matches_the_tables_tokenizer(term):
    # Measured against the engine, not asserted: the same tokenizer memory_fts uses.
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute('CREATE VIRTUAL TABLE t USING fts5(c, tokenize="porter ascii")')
        await db.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, instance)")
        await db.execute("INSERT INTO t VALUES (?)", [term])
        (count,) = (await db.execute_fetchall("SELECT count(*) FROM v"))[0]
    finally:
        await db.close()
    assert fts5_tokens(term) == count


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


# ── round 2: the lane base is judged by every word it would send ──────────────


async def test_lane_base_of_a_long_stopword_paste_is_bounded():
    """Round-2 review: the lane base was judged by the meaningful terms only, so a
    paste of thousands of stopwords (no meaningful terms) kept the raw prompt and
    reached FTS5 whole."""
    composed, _ = await _compose(" ".join(["the"] * 2000), extra=["needle1"])
    assert composed == "(the) OR (needle1)"


async def test_lane_base_of_a_mostly_stopword_paste_keeps_its_meaningful_terms():
    composed, _ = await _compose(
        " ".join(["the is of"] * 500) + " spill retrieval", extra=["needle1"]
    )
    assert composed == "(spill retrieval) OR (needle1)"


def test_fts5_query_tokens_counts_every_word_as_written():
    assert fts5_query_tokens("the a of") == 3  # stopwords and short words count
    assert fts5_query_tokens("foo_bar baz") == 3  # snake_case counts per piece
    assert fts5_query_tokens("") == 0


# ── class audit: the strict AND first pass is bounded too ─────────────────────


def test_the_and_pass_of_a_short_query_is_untouched():
    query = "graph expansion graph"
    assert and_pass(query) is query


def test_the_and_pass_of_a_long_query_is_de_duplicated():
    assert and_pass(" ".join(["memory session"] * 200)) == "memory session"


async def test_a_repeated_word_paste_matches_the_same_rows_without_the_repeats():
    """Class audit before round 3: the AND first pass took the whole expression,
    and FTS5 scores every repetition (measured 400 operands 12.7 s vs 0.04 s
    de-duplicated). De-duplicating keeps the matched rows identical."""
    db = await _corpus()
    sent = []
    real = db.execute_fetchall

    async def spy(sql, params):
        sent.append(params[0])
        return await real(sql, params)

    try:
        sql = "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank"
        long = " ".join(["common needle1"] * 300)
        once = await real(sql, ["common needle1"])
        db.execute_fetchall = spy
        got = await _fts.fetch_fts(db, sql, [long])
    finally:
        db.execute_fetchall = real
        await db.close()
    assert sent == ["common needle1"]  # FTS5 received each word once
    assert sorted(r[0] for r in got) == sorted(r[0] for r in once) and got


# ── round 3: the AND pass is capped, and split as the tokenizer splits ────────


def test_the_and_pass_caps_distinct_terms_at_the_budget():
    """Round-3 review: de-duplicating removed nothing from 33+ distinct words, so
    the strict pass still sent the whole prompt."""
    words = [f"tok{i}" for i in range(200)]
    capped = and_pass(" ".join(words))
    assert sum(fts5_tokens(t) for t in capped.split(" ")) <= N
    assert capped.split(" ") == words[:N]  # all tie at count 1: first appearance


def test_the_and_pass_keeps_a_non_ascii_space_inside_its_token():
    """Round-3 review: str.split() also splits on NBSP, which the ascii tokenizer
    treats as part of a token, so re-joining with spaces changed the matched
    tokens. Split on ASCII whitespace only."""
    nbsp = "foo\u00a0bar"
    query = " ".join([nbsp] * 40)
    assert and_pass(query) == nbsp
    assert _fts.operands(f"a {nbsp}\tb") == ["a", nbsp, "b"]


def test_the_or_retry_keeps_a_non_ascii_space_inside_its_token():
    """Same class, the OR retry: a lone NBSP-joined token is one operand, so there
    is nothing to OR."""
    assert or_fallback("foo\u00a0bar") is None
    assert or_fallback("foo\u00a0bar baz") == "foo\u00a0bar OR baz"


def test_the_and_pass_and_the_or_retry_share_one_term_list():
    """Premise check before round 4: the capped AND kept stopwords while the OR
    retry dropped them, so a long prose paste became an AND of mostly stopwords
    that matched rows and never reached the retry. Both now use one list."""
    prose = " ".join(["the cache is in the disk of the box"] * 30 + ["spill retrieval"])
    anded = and_pass(_prepare_fts5(prose))
    assert anded.split(" ") == or_fallback(_prepare_fts5(prose)).split(" OR ")
    assert not {"the", "is", "in", "of"} & set(anded.split(" "))
