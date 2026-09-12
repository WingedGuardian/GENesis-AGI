"""Shared unique-prefix id resolver for MCP tools keyed on Genesis-generated ids.

Most Genesis ids are ``uuid4().hex`` (32-char) or dashed UUIDs (36-char). The
proactive hook, ``memory_expand``, and ``session_charter`` all accept short hex
handles, so the ecosystem *teaches* prefix usage — but most id-taking tools do
exact-match lookups and reject a prefix with a soft "not found" that a caller
skims past. That mismatch silently lost a hard-dated commitment (the July 2026
graph-bake-off row: an 8-char prefix passed to ``follow_up_update``).

This mirrors ``genesis.mcp.memory.core._resolve_id_prefixes`` /
``genesis.db.crud.memory.match_id_prefix``: a unique prefix resolves; an
ambiguous prefix is never guessed; full-length and non-hex ids pass through
as the NORMALIZED id (an ``id:`` tag / whitespace / case stripped, never
prefix-matched); DB errors fail open to that same normalized id.
"""

from __future__ import annotations

import logging
import re

import aiosqlite

logger = logging.getLogger(__name__)

# Outcome constants (callers switch on these).
RESOLVED = "resolved"  # exactly one match — matches == [full_id]
AMBIGUOUS = "ambiguous"  # >1 match — matches == candidate ids (never guessed)
NOT_FOUND = "not_found"  # zero matches for a prefix-shaped id — matches == []
PASSTHROUGH = "passthrough"  # not prefix-shaped (full/non-hex) — matches == [mid] (normalized)

# table/id_column are ALWAYS developer-supplied literals, never caller input.
# Guarded so they can never reach f-string interpolation as anything but a plain
# identifier (defence-in-depth against a future misuse).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _prefix_re(min_len: int) -> re.Pattern[str]:
    # Hex, optionally dashed, at least min_len chars — a partial id. Anything
    # else (full ids, non-hex) bypasses resolution untouched.
    return re.compile(rf"^[0-9a-f][0-9a-f-]{{{min_len - 1},}}$")


async def resolve_unique_prefix(
    db: aiosqlite.Connection,
    *,
    table: str,
    id_column: str,
    raw_id: str,
    full_len: int = 32,
    min_len: int = 4,
) -> tuple[list[str], str]:
    """Resolve ``raw_id`` against ``{table}.{id_column}`` by unique prefix.

    Returns ``(matches, outcome)``:
      * ``RESOLVED``    → ``matches == [full_id]``
      * ``AMBIGUOUS``   → ``matches`` are the candidate ids (>=2, for the error)
      * ``NOT_FOUND``   → ``matches == []`` (prefix-shaped but nothing matched)
      * ``PASSTHROUGH`` → ``matches == [mid]`` — the NORMALIZED id (an ``id:`` tag,
        whitespace, and case stripped); full-length / non-hex / too short, so never
        prefix-matched, but returned normalized so an exact lookup still hits.
    """
    if not _IDENT_RE.match(table) or not _IDENT_RE.match(id_column):
        raise ValueError(f"unsafe identifier: table={table!r} id_column={id_column!r}")

    mid = raw_id.strip().lower().removeprefix("id:")
    if len(mid) >= full_len or not _prefix_re(min_len).match(mid):
        return [mid], PASSTHROUGH

    try:
        # LIMIT 3 → distinguish unique (1) from ambiguous (>=2) AND let the caller
        # name two candidates plus "possibly more" in an ambiguity error.
        cursor = await db.execute(
            f"SELECT {id_column} FROM {table} WHERE {id_column} LIKE ? || '%' LIMIT 3",
            (mid,),
        )
        matches = [str(r[0]) for r in await cursor.fetchall()]
    except Exception:
        logger.debug("prefix resolution failed for %r on %s", raw_id, table, exc_info=True)
        return [mid], PASSTHROUGH

    if len(matches) == 1:
        return matches, RESOLVED
    if matches:
        return matches, AMBIGUOUS
    return [], NOT_FOUND
