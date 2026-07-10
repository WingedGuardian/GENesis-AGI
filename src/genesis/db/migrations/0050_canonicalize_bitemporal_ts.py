"""Canonicalize memory_metadata.valid_at / invalid_at to sortable form.

Every bitemporal read is a raw SQLite TEXT comparison against
``datetime.now(UTC).isoformat()``, so non-canonical rows compare
incorrectly: ``Z`` sorts after ``+``, a space separator sorts before
``T``, naive timestamps sort before their aware twins, and non-UTC
offsets order by offset string instead of instant. Live census
(2026-07-09): valid_at — 2,632 Z-suffix, 67 naive-T, 11 non-UTC
offsets, 10 space/other (LLM temporal strings incl. ranges and words);
invalid_at — 55 space-format. Date-only valid_at (5,488) is canonical
by design ("valid from that date") and untouched.

Rules, applied via ``genesis.db.timeutil.canonical_iso`` plus two
data-repair cases for LLM temporal strings:
- parseable → canonical UTC ISO (``...T...+00:00``)
- range ("A/B", "A to B") → start date, date-only form
- month ("YYYY-MM") → first of month, date-only form
- unparseable ("Friday", free text) → NULL (honest "unknown onset";
  the always-on filter treats NULL invalid_at as still-valid, and
  valid_at is not filtered on today)

Data-only migration: no DDL, idempotent (canonical rows are no-ops on
re-run). Closes follow-up b80d00c7.
"""

from __future__ import annotations

import re

import aiosqlite

from genesis.db.timeutil import canonical_iso

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_RANGE_SPLIT_RE = re.compile(r"\s+to\s+|/")


def _repair(value: str) -> str | None:
    """Canonical form for one legacy value, or None if unsalvageable."""
    canonical = canonical_iso(value)
    if canonical is not None:
        return canonical
    text = value.strip()
    if _MONTH_RE.match(text):
        return f"{text}-01"
    start = _RANGE_SPLIT_RE.split(text)[0].strip()
    if _DATE_ONLY_RE.match(start):
        return start
    return None


async def up(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_metadata'"
    )
    if not await cursor.fetchone():
        return
    for column in ("valid_at", "invalid_at"):
        rows = await db.execute_fetchall(
            f"SELECT memory_id, {column} FROM memory_metadata "  # noqa: S608
            f"WHERE {column} IS NOT NULL"
        )
        for memory_id, value in rows:
            if _DATE_ONLY_RE.match(value) or (
                "T" in value and value.endswith("+00:00")
            ):
                continue  # already canonical
            await db.execute(
                f"UPDATE memory_metadata SET {column} = ? "  # noqa: S608
                f"WHERE memory_id = ?",
                (_repair(value), memory_id),
            )


async def down(db: aiosqlite.Connection) -> None:
    # Lossy-forward data repair; original strings are not preserved.
    return
