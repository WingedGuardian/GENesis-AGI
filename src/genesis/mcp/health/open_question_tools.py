"""open_question_raise / _resolve / _block / _list — the open-questions store.

An open question is an unresolved fork that needs the owner's judgement: it
does not belong on the work board (which holds confirmed work only), and it is
not a follow-up (which is work). It lives here, LOCAL ONLY — nothing in this
module ever touches GitHub — and it can BLOCK work as a graph edge:

* TODAY a block is ADVISORY: ``open_question_list(target=...)`` answers whether
  a record is blocked, and the morning report counts unverified questions.
  Once board promotion lands, it will REFUSE to promote a blocked ledger row or
  follow-up, and a blocked card will show the block (never moving the card).
  No path refuses anything yet — do not rely on one.

A session parks a genuine, non-urgent owner fork here rather than guessing
(triage bucket 4 of the question-triage protocol) — and, in a foreground
session, still TELLS the owner it parked one: the store is where the question
lives, not a substitute for asking. An assumption never counts as an answer:
resolve a question with what settled it.

Targets are written ``ledger:<id>``, ``follow_up:<id>`` or ``card:owner/repo#N``.
Ledger and follow-up ids accept a unique prefix (8+ hex), resolved against
their own table; an unknown or ambiguous id is refused, never guessed.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

_PREFIX = re.compile(r"^[0-9a-f]{8,32}$")
_TARGET_TABLES = {"ledger": "session_ledger", "follow_up": "follow_ups"}
# One literal query per table: no table name is ever interpolated into SQL.
_PREFIX_SQL = {
    "session_ledger": "SELECT id FROM session_ledger WHERE id LIKE ? LIMIT 2",
    "follow_ups": "SELECT id FROM follow_ups WHERE id LIKE ? LIMIT 2",
    "open_questions": "SELECT id FROM open_questions WHERE id LIKE ? LIMIT 2",
}


def _err(message: str) -> dict:
    return {"status": "error", "message": message}


async def _resolve_id(db, table: str, raw: str, label: str) -> tuple[str | None, str | None]:
    """(full_id, None) or (None, error). ``raw`` is a full id or a unique
    8+ hex prefix; the prefix is validated as hex first, so it is safe in LIKE."""
    raw = (raw or "").strip().lower()
    if not _PREFIX.match(raw):
        return None, f"{label} id must be 8-32 hex characters; got {raw!r}"
    cur = await db.execute(_PREFIX_SQL[table], (raw + "%",))
    rows = await cur.fetchall()
    if not rows:
        return None, f"no {label} with id {raw!r}"
    if len(rows) > 1:
        return None, f"{label} id prefix {raw!r} is ambiguous; give more characters"
    return rows[0][0], None


async def _parse_target(db, raw: str) -> tuple[tuple[str, str] | None, str | None]:
    from genesis.db.crud import board as board_crud

    kind, sep, value = (raw or "").strip().partition(":")
    if not sep or kind not in board_crud.TARGET_KINDS:
        return None, (
            f"target {raw!r} must be 'ledger:<id>', 'follow_up:<id>' or 'card:owner/repo#N'"
        )
    if kind == "card":
        try:
            return ("card", board_crud.normalize_target("card", value)), None
        except ValueError as exc:
            return None, str(exc)
    full, error = await _resolve_id(db, _TARGET_TABLES[kind], value, kind)
    if error:
        return None, error
    return (kind, full), None


async def _ready(db) -> str | None:
    from genesis.db.crud import board as board_crud

    if db is None:
        return "DB not initialized"
    if not await board_crud.tables_available(db):
        return "board tables not migrated yet (restart genesis-server to apply migrations)"
    return None


async def _impl_open_question_raise(
    db, *, question: str, context: str, blocks: list[str], raised_by: str, now: str
) -> dict:
    from genesis.db.crud import board as board_crud

    if (problem := await _ready(db)) is not None:
        return {"status": "unavailable", "message": problem}
    if len(blocks or []) > board_crud.MAX_BLOCKS:
        # Refused before any per-target lookup, so an oversized call costs nothing.
        return _err(f"{len(blocks)} blocks in one raise; the limit is {board_crud.MAX_BLOCKS}")
    parsed = []
    for raw in blocks or []:
        target, error = await _parse_target(db, raw)
        if error:
            return _err(error)  # validate EVERY target before writing anything
        parsed.append(target)
    try:
        # One transaction: the question and every edge land together, or none do.
        qid = await board_crud.raise_question(
            db,
            question=question,
            context=context or None,
            raised_by=raised_by or None,
            now=now,
            blocks=parsed,
        )
    except ValueError as exc:
        return _err(str(exc))
    return {"status": "ok", "question": await board_crud.get_question(db, qid)}


async def _impl_open_question_resolve(
    db, *, question_id: str, resolution: str, status: str, now: str
) -> dict:
    from genesis.db.crud import board as board_crud

    if (problem := await _ready(db)) is not None:
        return {"status": "unavailable", "message": problem}
    qid, error = await _resolve_id(db, "open_questions", question_id, "question")
    if error:
        return _err(error)
    try:
        changed = await board_crud.close_question(
            db, question_id=qid, status=status, resolution=resolution, now=now
        )
    except ValueError as exc:
        return _err(str(exc))
    question = await board_crud.get_question(db, qid)
    if not changed:
        return _err(f"question {qid} is already {question['status']}; it was not changed")
    return {"status": "ok", "question": question}


async def _impl_open_question_block(
    db, *, question_id: str, target: str, remove: bool, now: str
) -> dict:
    from genesis.db.crud import board as board_crud

    if (problem := await _ready(db)) is not None:
        return {"status": "unavailable", "message": problem}
    qid, error = await _resolve_id(db, "open_questions", question_id, "question")
    if error:
        return _err(error)
    parsed, error = await _parse_target(db, target)
    if error:
        return _err(error)
    kind, target_id = parsed
    try:
        if remove:
            changed = await board_crud.remove_block(
                db, question_id=qid, target_kind=kind, target_id=target_id
            )
        else:
            changed = await board_crud.add_block(
                db, question_id=qid, target_kind=kind, target_id=target_id, now=now
            )
    except ValueError as exc:
        return _err(str(exc))
    return {"status": "ok", "changed": changed, "question": await board_crud.get_question(db, qid)}


async def _impl_open_question_list(db, *, status: str, target: str, limit: int | None) -> dict:
    from genesis.db.crud import board as board_crud

    if (problem := await _ready(db)) is not None:
        return {"status": "unavailable", "message": problem}
    if target:
        parsed, error = await _parse_target(db, target)
        if error:
            return _err(error)
        kind, target_id = parsed
        blocking = await board_crud.blocking_questions(db, target_kind=kind, target_id=target_id)
        return {
            "status": "ok",
            "target": f"{kind}:{target_id}",
            "blocked": bool(blocking),
            "blocking_questions": blocking,
        }
    try:
        listing = await board_crud.list_questions(db, status=status or None, limit=limit)
    except ValueError as exc:
        return _err(str(exc))
    return {"status": "ok", **listing}


def _db_or_none():
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    return _service._db if _service is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


@mcp.tool()
async def open_question_raise(
    question: str, context: str = "", blocks: list[str] | None = None, raised_by: str = ""
) -> dict:
    """Park a genuine owner fork as an open question, optionally blocking work.

    For a decision that is truly the owner's (not answerable from evidence, not
    covered by a standing rule, not low-stakes and reversible) and not urgent.
    LOCAL ONLY — never posted to GitHub. In a foreground session, still tell
    the owner you parked it; a fork that blocks the work NOW is asked, not
    parked.

    ``blocks`` lists what the question blocks: ``ledger:<id>``,
    ``follow_up:<id>`` (full id or unique 8+ hex prefix) or
    ``card:owner/repo#N``. Today a block is advisory (read it back with
    ``open_question_list(target=...)``); board promotion will refuse a blocked
    record once it lands. ``raised_by`` names the asker (a session id or
    "owner").
    """
    return await _impl_open_question_raise(
        _db_or_none(),
        question=question,
        context=context,
        blocks=blocks or [],
        raised_by=raised_by,
        now=_now(),
    )


@mcp.tool()
async def open_question_resolve(
    question_id: str, resolution: str, status: str = "resolved"
) -> dict:
    """Close an open question: ``status`` is ``resolved`` (answered) or
    ``dropped`` (no longer matters). ``resolution`` is required — say what
    settled it; an assumption is not an answer. Closing releases every block
    the question held (the edges are kept as history)."""
    return await _impl_open_question_resolve(
        _db_or_none(), question_id=question_id, resolution=resolution, status=status, now=_now()
    )


@mcp.tool()
async def open_question_block(question_id: str, target: str, remove: bool = False) -> dict:
    """Add (or with ``remove=True`` drop) a block from an UNVERIFIED question to
    ``ledger:<id>``, ``follow_up:<id>`` or ``card:owner/repo#N``."""
    return await _impl_open_question_block(
        _db_or_none(), question_id=question_id, target=target, remove=remove, now=_now()
    )


@mcp.tool()
async def open_question_list(
    status: str = "unverified", target: str = "", limit: int | None = None
) -> dict:
    """List open questions (newest first, with their blocks; ``total`` is the
    full count for the filter, ``listed`` what this page holds). ``status`` is
    ``unverified`` / ``resolved`` / ``dropped``, or empty for all. With
    ``target`` (``ledger:<id>`` / ``follow_up:<id>`` / ``card:owner/repo#N``)
    it answers instead whether that target is blocked, and by which
    unverified questions."""
    return await _impl_open_question_list(_db_or_none(), status=status, target=target, limit=limit)
