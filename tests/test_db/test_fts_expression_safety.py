"""A composed FTS5 boolean expression must be one FTS5 will actually PARSE.

``_prepare_fts5(boolean=True)`` preserves ``AND``/``OR``/parentheses for
expressions built by the query expander, and strips every other character to
avoid FTS5 syntax errors. Its only structural safety check counts parenthesis
BALANCE -- but balance is not validity. Stripping happens AFTER the expression is
composed, so a term made only of punctuation reduces to whitespace and leaves a
DANGLING OPERATOR inside a still-balanced group::

    (graph AND expansion) OR ((graph OR expansion) AND (fusion OR  ))
                                                                 ^^ nothing here

FTS5 rejects that with ``fts5: syntax error near ")"``, which surfaced as
HTTP 500 from the recall endpoint on ordinary prompts.

Two independent producers compose such expressions, which is why the invariant is
pinned against FTS5 itself rather than against either producer's output shape:

* ``memory/intent.py::_build_expanded_query`` -- tag co-occurrence expansions,
  whose terms come from live indexed data and may hold arbitrary characters;
* ``memory/retrieval.py::_expand_fts_query`` -- the proactive hook's file-context
  keywords, OR-appended as ``f"({fts_query}) OR ({extra})"``. Its filter is
  ``if t``, which drops an EMPTY term but not a punctuation-only one.

The oracle here is SQLite's own parser. Asserting against a hand-written notion
of FTS5 grammar would only test that notion; the thing that has to hold is that
the real engine accepts the string.
"""

import sqlite3

import pytest

from genesis.db.crud.memory import _prepare_fts5
from genesis.memory.intent import _build_expanded_query
from genesis.memory.retrieval import HybridRetriever

# FTS5 may be unavailable in the in-memory SQLite build.
_fts5_available = True
try:
    _c = sqlite3.connect(":memory:")
    _c.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
    _c.close()
except Exception:
    _fts5_available = False

pytestmark = pytest.mark.skipif(not _fts5_available, reason="FTS5 not available")


@pytest.fixture
def fts():
    """A real FTS5 table -- the parser under test is SQLite's, not ours."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(content)")
    conn.execute("INSERT INTO t(content) VALUES ('graph expansion fusion weighting')")
    conn.commit()
    yield conn
    conn.close()


def _accepts(conn: sqlite3.Connection, expr: str | None) -> None:
    """Raises sqlite3.OperationalError if FTS5 will not parse ``expr``."""
    if expr is None:
        return  # a rejected-outright expression is a valid, safe outcome
    conn.execute("SELECT 1 FROM t WHERE t MATCH ?", (expr,)).fetchall()


# Expansion terms arrive from indexed data, so every one of these is reachable.
# The last entry is the NEGATIVE CONTROL: an ordinary hyphenated tag that must
# keep working, so a fix cannot pass by simply discarding expansions.
_EXPANSION_CASES = [
    pytest.param(["fusion", "→"], id="unicode-arrow-tag"),
    pytest.param(["fusion", ""], id="empty-tag"),
    pytest.param(["fusion", "-"], id="dash-only-tag"),
    pytest.param(["fusion", "..."], id="dots-only-tag"),
    pytest.param(["...", "///"], id="every-tag-punctuation-only"),
    pytest.param(["fusion", "co-occurrence"], id="NEGATIVE-CONTROL-hyphenated"),
]


@pytest.mark.parametrize("expansions", _EXPANSION_CASES)
def test_expanded_query_is_parseable_by_fts5(fts, expansions):
    """Producer 1: tag-expansion output must survive _prepare_fts5 as valid FTS5."""
    expr = _build_expanded_query(["graph", "expansion"], expansions)
    _accepts(fts, _prepare_fts5(expr, boolean=True))


@pytest.mark.parametrize(
    "extra_terms",
    [
        pytest.param(["retrieval.py", "---"], id="punctuation-only-file-keyword"),
        pytest.param(["...", "///"], id="all-file-keywords-punctuation"),
        pytest.param(["retrieval.py", "__init__.py"], id="NEGATIVE-CONTROL-real-filenames"),
    ],
)
async def test_file_keyword_append_is_parseable_by_fts5(fts, extra_terms):
    """Producer 2: the proactive hook's file-context keyword append.

    Calls the REAL ``HybridRetriever._build_fts_query`` rather than restating its
    composition here — a test that reimplements the code under test passes while
    production stays broken, which is exactly what an earlier draft of this test
    did. ``expand_query_terms=False`` keeps the expansion lane (and its Qdrant
    dependency) out of scope, so the method touches no instance state and can run
    unbound; the extras append is the whole point of this cell.
    """
    composed = await HybridRetriever._expand_fts_query(
        None,  # no self state is reachable with expand_query_terms=False
        query="graph expansion",
        collections=[],
        expand_query_terms=False,
        extra_fts_terms=extra_terms,
    )
    _accepts(fts, _prepare_fts5(composed, boolean=True))


def test_negative_control_expansion_still_gates_on_original_keywords():
    """A fix must not neuter expansion: the precision-preserving structure
    (audit MEM-001) must survive for ordinary terms -- expansion terms may only
    BOOST documents already matching an original keyword.
    """
    expr = _build_expanded_query(["graph", "expansion"], ["fusion", "weighting"])
    out = _prepare_fts5(expr, boolean=True)
    assert out is not None
    # both original keywords AND-gated, and the expansion terms present
    assert "graph AND expansion" in out
    assert "fusion" in out and "weighting" in out
    assert out.count("(") == out.count(")")


def test_punctuation_only_terms_do_not_leave_a_dangling_operator():
    """The specific structural defect, asserted directly so a regression names
    itself rather than surfacing as an opaque OperationalError.
    """
    expr = _build_expanded_query(["graph", "expansion"], ["fusion", "→"])
    out = _prepare_fts5(expr, boolean=True) or ""
    toks = out.replace(")", " ) ").replace("(", " ( ").split()
    # pairwise: deliberately one shorter than toks, so strict= must be False
    pairs = list(zip(toks, toks[1:], strict=False))
    assert not [(a, b) for a, b in pairs if a in ("OR", "AND", "NOT") and b == ")"], (
        f"operator immediately before ')' in {out!r}"
    )
    assert not [(a, b) for a, b in pairs if a == "(" and b == ")"], f"empty group in {out!r}"
