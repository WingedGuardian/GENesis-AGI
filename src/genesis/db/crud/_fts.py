"""Shared FTS5 query helpers.

FTS5's default MATCH syntax treats space-separated bare terms as an implicit
AND — every term must appear in a row. ``_prepare_fts5`` produces exactly such a
bare string, so a multi-word natural-language query requires ALL its tokens to
be present verbatim, else it returns nothing (a bare IP matches only because all
its tokens happen to be present). ``fetch_fts`` keeps the precise AND-first
behavior but falls back to an OR-join when AND finds nothing, so a verbose query
still recalls the best partial matches (BM25-ranked). It only ever ADDS results
when the AND query returned zero — it never changes a query that already hit.
"""

from __future__ import annotations

import logging
import re
import time

import aiosqlite

logger = logging.getLogger(__name__)


def fts5_term(value: object) -> str | None:
    """One FTS5-safe bare term, or ``None`` if nothing survives sanitising.

    Callers that COMPOSE a boolean expression must sanitise each term BEFORE
    joining it, not after. ``_prepare_fts5(boolean=True)`` strips unsafe
    characters from the finished expression, which turns a punctuation-only term
    into whitespace and leaves a dangling operator — ``(fusion OR  )`` — that is
    still parenthesis-BALANCED and so passes that function's only structural
    check, then fails in FTS5 with ``syntax error near ")"``.

    Returning ``None`` for "nothing left" is the point: it lets a composer DROP
    the term instead of emitting an operator with no operand. Word characters
    and single interior spaces survive (a hyphenated tag becomes two terms, the
    same result ``_prepare_fts5`` already produced for it); everything else goes.
    """
    if value is None:
        return None
    cleaned = re.sub(r"[^\w\s]", " ", str(value), flags=re.UNICODE)
    # Collapse runs so a joined expression can never contain a bare double space
    # that reads as an empty operand to a later reviewer.
    cleaned = " ".join(cleaned.split())
    return cleaned or None


# Ultra-common English stopwords dropped from the OR retry so a verbose query
# like "where is the nonexistent service" falls back to "nonexistent OR service"
# instead of "where OR is OR the OR ..." (which matches almost every row). Kept
# deliberately small — only near-universal function words — so it never strips a
# meaningful content term. The AND pass is unaffected (it keeps every token).
_OR_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "for",
        "from",
        "he",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "she",
        "that",
        "the",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    ]
)


def or_fallback(escaped: str) -> str | None:
    """The OR-joined form of a cleaned (implicit-AND) FTS5 query.

    Returns ``None`` for a single-term query (AND == OR, no fallback needed).
    A multi-term query becomes ``term1 OR term2 OR ...`` — the lowercase tokens
    are plain search terms and the uppercase ``OR`` is the FTS5 operator
    (``_prepare_fts5`` lowercases its input, so no accidental operators survive
    the join). Ultra-common stopwords are dropped from the OR join to keep the
    fallback precise; if EVERY token is a stopword they are all kept (an OR of
    stopwords still beats returning nothing).
    """
    parts = escaped.split()
    if len(parts) <= 1:
        return None
    meaningful = [p for p in parts if p not in _OR_STOPWORDS]
    return " OR ".join(meaningful or parts)


async def fetch_fts(
    db: aiosqlite.Connection,
    sql: str,
    params: list,
    *,
    boolean: bool = False,
    match_index: int = 0,
) -> list:
    """Run an FTS5 MATCH query, retrying OR-joined when AND returns nothing.

    Callers MUST pass a lowercased, operator-free MATCH expression (as
    ``_prepare_fts5`` produces) so the OR-join can never turn a bare token into
    an FTS5 operator. ``params[match_index]`` MUST be the MATCH expression.
    The OR retry fires only when the first (AND) pass returned zero rows, the
    expression was not already a structured boolean query (``boolean`` is
    False), and it is multi-term. The retry runs on a COPY of ``params`` so the
    caller's list is never mutated.
    """
    rows = await db.execute_fetchall(sql, params)
    if rows or boolean:
        return rows
    alt = or_fallback(params[match_index])
    if alt is None:
        return rows
    retry = list(params)
    retry[match_index] = alt
    return await db.execute_fetchall(sql, retry)


# ---------------------------------------------------------------------------
# BM25-inert term pruning for top-level disjunctions.
#
# FTS5's built-in bm25 computes a per-phrase IDF of
# ``log((N - n + 0.5) / (n + 0.5))`` and CLAMPS it to 1e-6 when that is <= 0,
# i.e. whenever the phrase occurs in at least half the rows (n >= N/2). Such a
# phrase contributes essentially nothing to any row's rank — MEASURED on a
# 100,481-row corpus: a lone ``memory`` (in every row) or ``genesis`` (51% of
# rows) ranks at about -2e-6, where ``infrastructure`` (32%) ranks at -1.39 —
# but OR-ing it into a MATCH still forces FTS5 to materialise and bm25-score
# every row it touches before ``ORDER BY rank LIMIT`` can cut. On this corpus
# the structural tag tokens (``memory``, ``class``, ``fact``, ``wing``) sit in
# 98–100% of rows, so a single such term in a disjunction turns a selective
# query into a scan-and-score of the whole corpus.
#
# Dropping an inert term from a TOP-LEVEL disjunction is rank-neutral up to
# that ~1e-6-per-phrase contribution: the only rows it loses are rows that
# matched nothing else, and those carried a ~0 score below every row that did.
# It is NOT neutral inside an AND (there an always-true operand narrows the
# set when removed), so callers must apply this only to OR-joined operands.
# ---------------------------------------------------------------------------

# Document frequencies drift slowly (a common term does not become rare in an
# hour), so they are cached per process. The TTL bounds how long a term that
# crossed the N/2 line keeps its old classification — the only consequence of
# staleness is a slower (still correct) query or a pruned term whose bm25
# weight is near the clamp either way.
_DF_TTL_S = 3600.0
_DF_CACHE_MAX = 20_000  # bound on distinct cached terms (≈ a few MB worst case)
_df_cache: dict[tuple[str, str], tuple[int, float]] = {}
_total_cache: dict[str, tuple[int, float]] = {}


def _reset_df_cache() -> None:
    """Clear the document-frequency cache (tests; the cache is process-global)."""
    _df_cache.clear()
    _total_cache.clear()


# A corpus-size move larger than this fraction since the frequencies were
# cached drops them all (see ``_fts_total_rows``). 1%: at 100k rows that is
# 1,000 rows — far below the shift needed to move a term across the N/2 line
# by more than a sliver, and far above day-to-day store churn.
_DF_INVALIDATE_FRACTION = 0.01


async def _fts_total_rows(db: aiosqlite.Connection, table: str, now: float) -> int:
    """The CURRENT row count, read live on every call.

    ``<table>_docsize`` holds exactly one row per indexed document and counts in
    ~1ms where ``count(*)`` on the FTS table itself scans (~90ms at 100k rows).
    It is an FTS5 shadow table that only exists with the default
    ``columnsize=1``; fall back to the direct count if it is absent.

    Reading it live is what bounds df-cache staleness: a bulk delete or insert
    that moves the corpus size by more than ``_DF_INVALIDATE_FRACTION`` since
    the frequencies were cached drops every cached frequency for the table, so
    a term that stopped being inert is re-measured on the next call instead of
    being pruned on counts up to ``_DF_TTL_S`` old. (Balanced delete+insert
    churn that leaves the size unchanged is still bounded only by the TTL.)
    """
    try:
        rows = await db.execute_fetchall(f"SELECT count(*) FROM {table}_docsize")  # noqa: S608
    except Exception:
        rows = await db.execute_fetchall(f"SELECT count(*) FROM {table}")  # noqa: S608
    total = int(rows[0][0]) if rows else 0
    hit = _total_cache.get(table)
    if hit is not None:
        cached_total = hit[0]
        moved = abs(total - cached_total) > _DF_INVALIDATE_FRACTION * max(cached_total, 1)
        if moved or now - hit[1] >= _DF_TTL_S:
            for key in [k for k in _df_cache if k[0] == table]:
                del _df_cache[key]
            _total_cache[table] = (total, now)
    else:
        _total_cache[table] = (total, now)
    return total


async def drop_bm25_inert_terms(
    db: aiosqlite.Connection,
    terms: list[str],
    *,
    table: str = "memory_fts",
) -> list[str]:
    """Return ``terms`` minus those FTS5's bm25 treats as inert (df >= N/2).

    ``terms`` must be already-sanitised bare terms (``fts5_term`` output) that
    the caller is about to OR-join at the TOP level of a MATCH expression — see
    the module note above for why this is rank-neutral there and wrong inside
    an AND. The document frequency of each term is measured by FTS5 itself
    (``MATCH`` on the term), so tokenisation and stemming are exactly the
    engine's — no Python re-implementation of the porter stemmer to drift.

    May return an empty list when every term is inert; the caller then drops
    the whole disjunction (it could only ever add ~0-scored rows). Fails OPEN:
    any lookup error returns ``terms`` unchanged, since this is purely a cost
    optimisation and the unpruned query is the pre-existing behaviour.
    """
    if not terms:
        return terms
    now = time.monotonic()
    try:
        total = await _fts_total_rows(db, table, now)
        if total <= 0:
            return terms
        kept: list[str] = []
        for term in terms:
            key = (table, term)
            hit = _df_cache.get(key)
            if hit is None or now - hit[1] >= _DF_TTL_S:
                rows = await db.execute_fetchall(
                    f"SELECT count(*) FROM {table} WHERE {table} MATCH ?",  # noqa: S608
                    (term,),
                )
                df = int(rows[0][0]) if rows else 0
                if len(_df_cache) >= _DF_CACHE_MAX:
                    _df_cache.clear()
                _df_cache[key] = (df, now)
            else:
                df = hit[0]
            if df * 2 < total:
                kept.append(term)
        return kept
    except Exception:
        logger.debug("bm25-inert term lookup failed — keeping all terms", exc_info=True)
        return terms
