"""CRUD for ``cognitive_file_modifications`` — the cognitive self-mod ledger.

Thin, strict data layer. Append-only ledger of cognitive-config file overwrites
(skill ``SKILL.md`` / ``TRIAGE_CALIBRATION.md`` / ``USER_KNOWLEDGE.md``), each row
capturing the PRE-IMAGE so the write can be rolled back. ``record`` raises
``ValueError`` on malformed input; the best-effort leniency that protects the live
cognitive write paths lives one layer up in ``learning/cognitive_ledger.py``.

NOT the (dead, CC-tool-audit) ``file_modifications`` table — different scope, and
this one stores ``prior_content``/``applied_content``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)

_VALID_STATUSES = frozenset({"applied", "rolled_back"})


def _row_to_dict(cur: aiosqlite.Cursor, row) -> dict:
    """Map a row to a dict, parsing ``metadata`` JSON back to a dict."""
    cols = [d[0] for d in cur.description]
    out = dict(zip(cols, row, strict=False))
    if out.get("metadata"):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            out["metadata"] = json.loads(out["metadata"])
    return out


async def record(
    db: aiosqlite.Connection,
    *,
    actor: str,
    target_path: str,
    applied_content: str,
    prior_content: str | None = None,
    change_summary: str | None = None,
    metadata: dict | None = None,
    created_at: str | None = None,
) -> str:
    """Insert one ledger row (a captured cognitive file overwrite). Returns its id.

    Raises ``ValueError`` on empty ``actor``/``target_path`` or ``None``
    ``applied_content`` (the column is NOT NULL — a missing post-image is a bug).
    """
    if not actor or not target_path:
        raise ValueError("cognitive_file_modifications.record: actor/target_path required")
    if applied_content is None:
        raise ValueError("cognitive_file_modifications.record: applied_content must not be None")

    mod_id = uuid.uuid4().hex[:16]
    created_at = created_at or datetime.now(UTC).isoformat()
    meta_json = json.dumps(metadata) if metadata is not None else None

    await db.execute(
        """INSERT INTO cognitive_file_modifications
               (id, actor, target_path, prior_content, applied_content,
                change_summary, metadata, status, created_at, rolled_back_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'applied', ?, NULL)""",
        (
            mod_id, actor, target_path, prior_content, applied_content,
            change_summary, meta_json, created_at,
        ),
    )
    await db.commit()
    return mod_id


async def get(db: aiosqlite.Connection, mod_id: str) -> dict | None:
    """Fetch one ledger row by id (metadata parsed to a dict)."""
    cur = await db.execute(
        "SELECT * FROM cognitive_file_modifications WHERE id = ?", (mod_id,),
    )
    row = await cur.fetchone()
    return _row_to_dict(cur, row) if row else None


async def recent(
    db: aiosqlite.Connection, *, limit: int = 20, actor: str | None = None,
) -> list[dict]:
    """Most-recent ledger rows, newest first, optionally filtered by actor."""
    if actor:
        cur = await db.execute(
            "SELECT * FROM cognitive_file_modifications WHERE actor = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (actor, limit),
        )
    else:
        cur = await db.execute(
            "SELECT * FROM cognitive_file_modifications "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    rows = await cur.fetchall()
    return [_row_to_dict(cur, r) for r in rows]


async def counts_by_target(db: aiosqlite.Connection) -> list[dict]:
    """Per-target row counts + last-modified timestamp (operator overview)."""
    cur = await db.execute(
        "SELECT target_path, COUNT(*) AS n, MAX(created_at) AS last_at, "
        "       SUM(CASE WHEN status = 'rolled_back' THEN 1 ELSE 0 END) AS rolled_back "
        "FROM cognitive_file_modifications GROUP BY target_path ORDER BY last_at DESC",
    )
    rows = await cur.fetchall()
    return [_row_to_dict(cur, r) for r in rows]


async def mark_rolled_back(
    db: aiosqlite.Connection, mod_id: str, *, rolled_back_at: str | None = None,
) -> bool:
    """Mark a row rolled back. Returns True iff a row was updated."""
    rolled_back_at = rolled_back_at or datetime.now(UTC).isoformat()
    cur = await db.execute(
        "UPDATE cognitive_file_modifications "
        "SET status = 'rolled_back', rolled_back_at = ? WHERE id = ?",
        (rolled_back_at, mod_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def prune_keep_per_target(
    db: aiosqlite.Connection, target_path: str, *, keep: int,
) -> int:
    """Delete all but the most-recent ``keep`` rows for ``target_path``.

    Bounds table growth (rows store full file contents). Returns rows deleted.
    """
    cur = await db.execute(
        """DELETE FROM cognitive_file_modifications
           WHERE target_path = ? AND id NOT IN (
               SELECT id FROM cognitive_file_modifications
               WHERE target_path = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?
           )""",
        (target_path, target_path, keep),
    )
    await db.commit()
    return cur.rowcount
