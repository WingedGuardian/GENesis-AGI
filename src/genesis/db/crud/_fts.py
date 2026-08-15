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

import aiosqlite


def or_fallback(escaped: str) -> str | None:
    """The OR-joined form of a cleaned (implicit-AND) FTS5 query.

    Returns ``None`` for a single-term query (AND == OR, no fallback needed).
    A multi-term query becomes ``term1 OR term2 OR ...`` — the lowercase tokens
    are plain search terms and the uppercase ``OR`` is the FTS5 operator
    (``_prepare_fts5`` lowercases its input, so no accidental operators survive
    the join).
    """
    parts = escaped.split()
    return " OR ".join(parts) if len(parts) > 1 else None


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
