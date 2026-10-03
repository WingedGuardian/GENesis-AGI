"""CRUD for the work board's own stores.

Three stores, one module (the spec allows exactly three, and they are read
together): ``board_links`` (card <-> private source pointers + promotion audit),
``open_questions`` + ``open_question_blocks`` (the local-only question graph),
and ``board_events`` (the append-only event log). Why each exists and why no
existing store could hold it: the ``20261003010926_board_stores`` migration
docstring.

Conventions shared with the other CRUD modules:

* ``now`` is always injected (ISO-8601 UTC), never read from the wall clock, so
  behaviour is deterministic under test.
* Every closed vocabulary is validated HERE, because this module owns the
  columns: a caller that passes an unknown value gets a ``ValueError`` naming
  the valid set, never a row the readers then have to guess about.
* Subprocess writers do not run migrations, so callers that can run before the
  server has migrated check :func:`tables_available` first.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

import aiosqlite

TABLES = ("board_links", "open_questions", "open_question_blocks", "board_events")

SOURCE_KINDS = ("ledger", "follow_up")
TARGET_KINDS = ("ledger", "follow_up", "card")
QUESTION_STATUSES = ("unverified", "resolved", "dropped")
TERMINAL_QUESTION_STATUSES = ("resolved", "dropped")

#: The closed event vocabulary. No CHECK constraint in the schema (SQLite cannot
#: ALTER one, and the set grows when dispatch lands), so this frozenset IS the
#: constraint. Each name says what was OBSERVED or DONE, never what it means:
#:   promotion          — a private record became a public issue (pointer written)
#:   promotion_refused  — promotion stopped (an open-question block, a scan refusal)
#:   item_added         — Genesis added an existing public item to the project
#:   external_marked    — a third-party item was marked "promote to work it"
#:   drag               — a Status change Genesis did not make was observed
#:   status_write       — Genesis wrote a project field (audit; never a trigger)
#:   bookkeeping_move   — Genesis moved a card (the one allowed move: In Review)
#:   comment_write      — Genesis posted a templated, scanned comment
#:   override           — an owner move past an advisory (admission, blocked-by)
EVENTS = frozenset(
    {
        "promotion",
        "promotion_refused",
        "item_added",
        "external_marked",
        "drag",
        "status_write",
        "bookkeeping_move",
        "comment_write",
        "override",
    }
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
# owner/repo#123 — GitHub's own owner/name grammar (alnum, '-', '_', '.').
_CARD = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*)/[A-Za-z0-9._-]+#[1-9][0-9]*$")
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*)/[A-Za-z0-9._-]+$")

#: Free-text ceilings, enforced HERE because this module owns the columns
#: (the pr_verifications precedent: a writer that trusts its callers to bound
#: a column is not bounding it). Sized for prose a human writes by hand; a
#: value over the bound is REFUSED, never cut.
MAX_QUESTION_CHARS = 4000
MAX_CONTEXT_CHARS = 16000
MAX_RESOLUTION_CHARS = 16000
MAX_DETAIL_BYTES = 64 * 1024
#: Short identifying labels (who raised it, which worker, a GitHub node id, an
#: observed-change key). Sized well above any real value: a session id is 36,
#: a project item node id ~25, a change key an id plus an ISO timestamp.
MAX_LABEL_CHARS = 200
MAX_REASON_CHARS = 4000
#: Edges per question in one raise. Real questions block one to a handful of
#: things; the bound keeps one raise from holding the database write lock long.
MAX_BLOCKS = 50


async def _all(cur: aiosqlite.Cursor) -> list[dict]:
    """Rows as dicts via ``cursor.description`` — independent of the caller's
    ``row_factory`` (this module never mutates a shared connection)."""
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in await cur.fetchall()]


async def _one(cur: aiosqlite.Cursor) -> dict | None:
    names = [d[0] for d in cur.description]
    row = await cur.fetchone()
    return None if row is None else dict(zip(names, row, strict=True))


def _check_len(name: str, value: str | None, limit: int) -> None:
    if value is not None and len(value) > limit:
        raise ValueError(f"{name} is {len(value)} chars; the limit is {limit}")


def _one_of(name: str, value: str, valid: tuple[str, ...] | frozenset[str]) -> None:
    if value not in valid:
        raise ValueError(f"{name} must be one of {sorted(valid)}; got {value!r}")


def validate_target(kind: str, target_id: str) -> None:
    """Refuse a malformed block target. Ledger rows and follow-ups are keyed by
    their full 32-hex id (resolve a short prefix BEFORE calling); a card is
    ``owner/repo#N``."""
    _one_of("target_kind", kind, TARGET_KINDS)
    if kind == "card":
        if not _CARD.match(target_id or ""):
            raise ValueError(f"card target must look like owner/repo#123; got {target_id!r}")
    elif not _HEX32.match(target_id or ""):
        raise ValueError(f"{kind} target must be a full 32-hex id; got {target_id!r}")


def normalize_target(kind: str, target_id: str) -> str:
    """Validate, then return the canonical stored form: a card's ``owner/repo``
    is lowercased (GitHub names are case-insensitive, so ``O/R#5`` and
    ``o/r#5`` must be one edge, not two); ids pass through unchanged."""
    validate_target(kind, target_id)
    if kind == "card":
        repo, _, number = target_id.partition("#")
        return f"{repo.lower()}#{number}"
    return target_id


async def tables_available(db: aiosqlite.Connection) -> bool:
    """True when every board table exists (the migration has run)."""
    placeholders = ",".join("?" for _ in TABLES)
    cur = await db.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
        TABLES,
    )
    row = await cur.fetchone()
    return bool(row) and row[0] == len(TABLES)


# ─── board_links ────────────────────────────────────────────────────────────


# GROUNDWORK(board-promotion): the promotion drain writes this pointer after it
# creates or adopts the public issue (branch feat/board-promotion).
async def record_link(
    db: aiosqlite.Connection,
    *,
    source_kind: str,
    source_id: str,
    repo: str,
    issue_number: int,
    promoted_by: str,
    scan_receipt: dict,
    body_sha256: str,
    now: str,
    adopted: bool = False,
    approval_id: str | None = None,
    project_item_id: str | None = None,
) -> dict:
    """Write the pointer for a promotion that HAS happened (the issue exists).

    Idempotent for the same (source, issue) pair — a retried post-step returns
    the existing row, including when a concurrent writer inserted it first (the
    insert yields on conflict, then both keys are re-read). A DIFFERENT pairing
    for either side raises: one private record maps to one issue and vice versa,
    and silently keeping the first would hide a duplicate issue on GitHub.
    ``repo`` is stored lowercased, because GitHub owner/name is case-insensitive
    and a case variant must not escape the one-issue-one-pointer rule.
    """
    _one_of("source_kind", source_kind, SOURCE_KINDS)
    if not _HEX32.match(source_id or ""):
        raise ValueError(f"source_id must be a full 32-hex id; got {source_id!r}")
    if not _REPO.match(repo or ""):
        raise ValueError(f"repo must look like owner/name; got {repo!r}")
    repo = repo.lower()
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
        raise ValueError(f"issue_number must be a positive int; got {issue_number!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", body_sha256 or ""):
        raise ValueError("body_sha256 must be a 64-hex sha256 digest")
    if not promoted_by:
        raise ValueError("promoted_by is required (who approved the promotion)")
    if not now:
        raise ValueError("now is required")

    link_id = uuid.uuid4().hex
    async with _write_unit(db, "record_link"):
        await db.execute(
            "INSERT INTO board_links (id, source_kind, source_id, repo, issue_number, "
            "project_item_id, adopted, promoted_by, approval_id, scan_receipt, body_sha256, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            (
                link_id,
                source_kind,
                source_id,
                repo,
                issue_number,
                project_item_id,
                1 if adopted else 0,
                promoted_by,
                approval_id,
                json.dumps(scan_receipt, sort_keys=True),
                body_sha256,
                now,
                now,
            ),
        )
        by_source = await get_link_by_source(db, source_kind=source_kind, source_id=source_id)
        by_issue = await get_link_by_issue(db, repo=repo, issue_number=issue_number)
    if by_source is None and by_issue is None:
        raise RuntimeError(f"board link {source_kind}:{source_id} did not persist")
    if by_source is None or by_issue is None or by_source["id"] != by_issue["id"]:
        held = by_source or by_issue
        raise ValueError(
            f"conflicting board link: {source_kind}:{source_id} -> {repo}#{issue_number} "
            f"but {held['source_kind']}:{held['source_id']} -> "
            f"{held['repo']}#{held['issue_number']} already exists"
        )
    return by_source


def _link_row(out: dict | None) -> dict | None:
    if out is None:
        return None
    out["adopted"] = bool(out["adopted"])
    try:
        out["scan_receipt"] = json.loads(out["scan_receipt"])
    except (TypeError, ValueError):
        out["scan_receipt"] = {"unparseable": True}
    return out


# GROUNDWORK(board-promotion): promotion's duplicate check and drain re-link.
async def get_link_by_source(
    db: aiosqlite.Connection, *, source_kind: str, source_id: str
) -> dict | None:
    cur = await db.execute(
        "SELECT * FROM board_links WHERE source_kind = ? AND source_id = ?",
        (source_kind, source_id),
    )
    return _link_row(await _one(cur))


# GROUNDWORK(board-promotion): the one-issue-one-pointer re-read in record_link.
async def get_link_by_issue(
    db: aiosqlite.Connection, *, repo: str, issue_number: int
) -> dict | None:
    cur = await db.execute(
        "SELECT * FROM board_links WHERE repo = ? AND issue_number = ?",
        ((repo or "").lower(), issue_number),
    )
    return _link_row(await _one(cur))


# GROUNDWORK(board-reconciler): the reconciler records the card once it adds the
# issue to the project (the board PR after promotion).
async def set_project_item(
    db: aiosqlite.Connection, *, link_id: str, project_item_id: str, now: str
) -> bool:
    """Record the project item once the issue has been added to the board."""
    async with _write_unit(db, "set_project_item"):
        cur = await db.execute(
            "UPDATE board_links SET project_item_id = ?, updated_at = ? WHERE id = ?",
            (project_item_id, now, link_id),
        )
    return cur.rowcount == 1


# GROUNDWORK(board-reconciler): coverage numerator for the board status read.
async def count_links(db: aiosqlite.Connection) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM board_links")
    return (await cur.fetchone())[0]


# ─── open questions ─────────────────────────────────────────────────────────


class WriteBusy(Exception):
    """The write lock was lost on every attempt; NOTHING was written."""


async def _begin_immediate(db: aiosqlite.Connection, what: str) -> None:
    """Take the write lock before writing anything, retrying a lost lock race on
    the shared connection's schedule. This is the ONLY retried step: until it
    succeeds nothing has been written, so a retry can never repeat a write."""
    import asyncio
    import random

    from genesis.db.connection import (
        _JITTER_HIGH,
        _JITTER_LOW,
        _WRITE_RETRY_DELAYS,
        SerializedConnection,
        _is_lock_error,
    )

    if isinstance(db, SerializedConnection):
        raise TypeError(f"{what} needs a connection it owns (get_raw_db), not the shared one")
    if db.in_transaction:
        raise ValueError(f"{what} needs an idle connection; commit or roll back first")
    for delay in (*_WRITE_RETRY_DELAYS, None):
        try:
            await db.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc):
                raise
            if delay is None:
                raise WriteBusy(str(exc)) from exc
            await asyncio.sleep(delay * random.uniform(_JITTER_LOW, _JITTER_HIGH))


async def _commit(db: aiosqlite.Connection) -> None:
    """Commit. A LOCK error that leaves the connection OUT of its transaction
    is treated as committed: ``db/connection.py`` (``_retry_locked``) documents
    such a case (WAL's post-commit autocheckpoint losing the race after the
    frame is durable); whether SQLite can report it is not measured here, so
    this is a defensive reading. It is safe either way: a lock error that
    leaves no transaction open cannot mean the write was rolled back. Any other
    failure leaves the transaction open, and the caller rolls it back."""
    from genesis.db.connection import _is_lock_error

    try:
        await db.commit()
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc) and not db.in_transaction:
            return
        raise


@contextlib.asynccontextmanager
async def _write_unit(db: aiosqlite.Connection, what: str):
    """One board-store write (every writer in this module uses it) as ONE
    transaction on a connection the caller
    OWNS (``get_raw_db``), never the server's shared ``SerializedConnection``:
    that one serialises single statements but not a unit of them, and the
    health MCP middleware rolls it back after any failed tool call, so another
    call's commit or rollback could land between this unit's statements, split
    a question from its blocks, or discard a write already reported saved. The
    shared connection is refused outright. All or nothing: any exception in the
    body (CancelledError included) rolls back; only taking the lock is retried.

    One window remains, and it is aiosqlite's, not this unit's: a cancellation
    that lands while COMMIT is queued cancels only the WAIT, so the write is
    saved while the caller sees CancelledError (measured, aiosqlite 0.22.1).
    A raise has no idempotency key, so a caller that retries after a cancel
    can raise the same question twice."""
    await _begin_immediate(db, what)
    try:
        yield
        await _commit(db)
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise


async def raise_question(
    db: aiosqlite.Connection,
    *,
    question: str,
    now: str,
    context: str | None = None,
    raised_by: str | None = None,
    blocks: list[tuple[str, str]] | None = None,
) -> str:
    """Record a new unverified question, and the ``(kind, target_id)`` edges it
    blocks, in ONE transaction; returns its id. Every target is validated before
    anything is written, so a question can never land with only some of its
    blocks (which would under-block promotion). ``db`` must be an idle
    connection the caller owns (see :func:`_write_unit`)."""
    question = (question or "").strip()
    if not question:
        raise ValueError("question is required")
    if not now:
        raise ValueError("now is required")
    _check_len("question", question, MAX_QUESTION_CHARS)
    _check_len("context", context, MAX_CONTEXT_CHARS)
    _check_len("raised_by", raised_by, MAX_LABEL_CHARS)
    if blocks is not None and len(blocks) > MAX_BLOCKS:
        raise ValueError(f"{len(blocks)} blocks in one raise; the limit is {MAX_BLOCKS}")
    edges = sorted({(kind, normalize_target(kind, tid)) for kind, tid in (blocks or [])})
    qid = uuid.uuid4().hex
    async with _write_unit(db, "raise_question"):
        await db.execute(
            "INSERT INTO open_questions (id, question, context, status, raised_by, "
            "created_at, updated_at) VALUES (?,?,?,'unverified',?,?,?)",
            (qid, question, context, raised_by, now, now),
        )
        for kind, tid in edges:
            await db.execute(
                "INSERT INTO open_question_blocks (question_id, target_kind, target_id, "
                "created_at) VALUES (?,?,?,?)",
                (qid, kind, tid, now),
            )
    return qid


async def get_question(db: aiosqlite.Connection, question_id: str) -> dict | None:
    cur = await db.execute("SELECT * FROM open_questions WHERE id = ?", (question_id,))
    out = await _one(cur)
    if out is None:
        return None
    out["blocks"] = await blocks_of(db, question_id)
    return out


async def close_question(
    db: aiosqlite.Connection,
    *,
    question_id: str,
    status: str,
    resolution: str,
    now: str,
) -> bool:
    """Move an UNVERIFIED question to resolved/dropped. Returns False when the
    question is unknown or already closed — the transition is guarded on the
    observed status, so a race is a no-op, never a clobber of the first answer.
    Closing a question releases its blocks (they stop counting) but keeps the
    edges, so the record shows what it had blocked."""
    from genesis.security.immunity_shadow import guard_human_gate

    # Answering an owner fork IS owner authority: a dispatched / unsupervised
    # session is refused here, below every MCP wrapper (DispatchGateRefused).
    guard_human_gate("open_question_resolve")
    _one_of("status", status, TERMINAL_QUESTION_STATUSES)
    resolution = (resolution or "").strip()
    if not resolution:
        raise ValueError("resolution is required: say what settled it")
    _check_len("resolution", resolution, MAX_RESOLUTION_CHARS)
    async with _write_unit(db, "close_question"):
        cur = await db.execute(
            "UPDATE open_questions SET status = ?, resolution = ?, updated_at = ?, closed_at = ? "
            "WHERE id = ? AND status = 'unverified'",
            (status, resolution, now, now, question_id),
        )
    return cur.rowcount == 1


async def add_block(
    db: aiosqlite.Connection,
    *,
    question_id: str,
    target_kind: str,
    target_id: str,
    now: str,
) -> bool:
    """Add a question -> work edge. Refused (ValueError) for an unknown or closed
    question: a settled question blocking new work would be a stale gate.
    Returns False when the edge already existed. The insert itself re-checks
    the question is still unverified, so a close landing between the check and
    the write cannot leave a fresh edge on a closed question."""
    target_id = normalize_target(target_kind, target_id)
    q = await get_question(db, question_id)
    if q is None:
        raise ValueError(f"unknown question {question_id!r}")
    if q["status"] != "unverified":
        raise ValueError(
            f"question {question_id} is {q['status']}; only an unverified question can block"
        )
    async with _write_unit(db, "add_block"):
        cur = await db.execute(
            "INSERT INTO open_question_blocks (question_id, target_kind, target_id, created_at) "
            "SELECT ?,?,?,? WHERE EXISTS (SELECT 1 FROM open_questions "
            "WHERE id = ? AND status = 'unverified') ON CONFLICT DO NOTHING",
            (question_id, target_kind, target_id, now, question_id),
        )
    return cur.rowcount == 1


async def remove_block(
    db: aiosqlite.Connection, *, question_id: str, target_kind: str, target_id: str
) -> bool:
    """Drop an edge from an UNVERIFIED question. A closed question's edges are
    its history (close_question keeps them), so they are never removed — the
    delete itself re-checks the status, so a close racing this cannot lose one.
    Unblocking work is owner authority: refused for a dispatched session."""
    from genesis.security.immunity_shadow import guard_human_gate

    guard_human_gate("open_question_unblock")
    target_id = normalize_target(target_kind, target_id)
    async with _write_unit(db, "remove_block"):
        cur = await db.execute(
            "DELETE FROM open_question_blocks WHERE question_id = ? AND target_kind = ? "
            "AND target_id = ? "
            "AND EXISTS (SELECT 1 FROM open_questions WHERE id = ? AND status = 'unverified')",
            (question_id, target_kind, target_id, question_id),
        )
    return cur.rowcount == 1


async def blocks_of(db: aiosqlite.Connection, question_id: str) -> list[dict]:
    cur = await db.execute(
        "SELECT target_kind, target_id, created_at FROM open_question_blocks "
        "WHERE question_id = ? ORDER BY created_at, target_kind, target_id",
        (question_id,),
    )
    return await _all(cur)


async def blocking_questions(
    db: aiosqlite.Connection, *, target_kind: str, target_id: str
) -> list[dict]:
    """The UNVERIFIED questions currently blocking a target — the input to
    promotion's hard refusal and the card advisory. Closed questions never
    count, whatever edges they still carry."""
    target_id = normalize_target(target_kind, target_id)
    cur = await db.execute(
        "SELECT q.id, q.question, q.created_at FROM open_question_blocks b "
        "JOIN open_questions q ON q.id = b.question_id "
        "WHERE b.target_kind = ? AND b.target_id = ? AND q.status = 'unverified' "
        "ORDER BY q.created_at, q.id",
        (target_kind, target_id),
    )
    return await _all(cur)


#: Ids per ``IN (...)`` query: below SQLite's oldest bound-variable limit (999).
_IN_CHUNK = 500


async def list_questions(
    db: aiosqlite.Connection,
    *,
    status: str | None = "unverified",
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    """One page of questions (newest first) with their blocks, plus the FULL
    total for the filter — ``listed`` vs ``total`` so a page is never mistaken
    for the whole set. ``status=None`` lists every status. Blocks for the whole
    page come back in ONE query, not one per question."""
    if status is not None:
        _one_of("status", status, QUESTION_STATUSES)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise ValueError("limit must be a positive int or None")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative int")
    where, params = ("WHERE status = ?", (status,)) if status is not None else ("", ())
    cur = await db.execute(f"SELECT COUNT(*) FROM open_questions {where}", params)
    total = (await cur.fetchone())[0]
    sql = f"SELECT * FROM open_questions {where} ORDER BY created_at DESC, id"
    if limit is not None:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    elif offset:
        sql += f" LIMIT -1 OFFSET {int(offset)}"
    cur = await db.execute(sql, params)
    items = await _all(cur)
    edges: dict[str, list[dict]] = {item["id"]: [] for item in items}
    ids = [item["id"] for item in items]
    # Chunked: an unpaged read (limit=None) must not outgrow SQLite's
    # bound-variable limit, which is 999 on older builds.
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        cur = await db.execute(
            "SELECT question_id, target_kind, target_id, created_at FROM open_question_blocks "
            f"WHERE question_id IN ({','.join('?' for _ in chunk)}) "
            "ORDER BY created_at, target_kind, target_id",
            chunk,
        )
        for edge in await _all(cur):
            edges[edge.pop("question_id")].append(edge)
    for item in items:
        item["blocks"] = edges[item["id"]]
    return {"items": items, "listed": len(items), "total": total, "offset": offset}


async def question_summary(db: aiosqlite.Connection) -> dict:
    """Counts only — ``{"unverified": n, "oldest_created_at": iso | None}`` —
    for surfaces that must never render question text (the morning report)."""
    cur = await db.execute(
        "SELECT COUNT(*), MIN(created_at) FROM open_questions WHERE status = 'unverified'"
    )
    count, oldest = await cur.fetchone()
    return {"unverified": count, "oldest_created_at": oldest}


# ─── board_events ───────────────────────────────────────────────────────────


# GROUNDWORK(board-promotion): promotion logs promotion / promotion_refused here;
# the reconciler adds the rest of EVENTS.
async def append_event(
    db: aiosqlite.Connection,
    *,
    event: str,
    now: str,
    repo: str | None = None,
    issue_number: int | None = None,
    project_item_id: str | None = None,
    attempt: int | None = None,
    worker: str | None = None,
    reason: str | None = None,
    observed_change_key: str | None = None,
    detail: dict | None = None,
) -> int | None:
    """Append one event. Returns its id, or None when ``observed_change_key``
    names a change this event type has already recorded (the partial UNIQUE
    index absorbs a re-read of the same GitHub change). Any OTHER failure raises
    — the conflict clause names that index only, so a missing value can never be
    mistaken for a dedup hit."""
    _one_of("event", event, EVENTS)
    if not now:
        raise ValueError("now is required")
    if repo is not None:
        if not _REPO.match(repo):
            raise ValueError(f"repo must look like owner/name; got {repo!r}")
        repo = repo.lower()
    for name, value in (("issue_number", issue_number), ("attempt", attempt)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{name} must be a positive int or None; got {value!r}")
    for name, value in (
        ("project_item_id", project_item_id),
        ("worker", worker),
        ("observed_change_key", observed_change_key),
    ):
        _check_len(name, value, MAX_LABEL_CHARS)
    _check_len("reason", reason, MAX_REASON_CHARS)
    detail_json = None
    if detail is not None:
        detail_json = json.dumps(detail, sort_keys=True)
        if len(detail_json.encode()) > MAX_DETAIL_BYTES:
            raise ValueError(
                f"detail is {len(detail_json.encode())} bytes; the limit is {MAX_DETAIL_BYTES}"
            )
    async with _write_unit(db, "append_event"):
        cur = await db.execute(
            "INSERT INTO board_events (event, repo, issue_number, project_item_id, attempt, "
            "worker, reason, observed_change_key, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (event, observed_change_key) WHERE observed_change_key IS NOT NULL "
            "DO NOTHING",
            (
                event,
                repo,
                issue_number,
                project_item_id,
                attempt,
                worker,
                reason,
                observed_change_key,
                detail_json,
                now,
            ),
        )
    return cur.lastrowid if cur.rowcount == 1 else None


# GROUNDWORK(board-promotion): read back by promotion's tests and the board status.
async def list_events(
    db: aiosqlite.Connection,
    *,
    repo: str | None = None,
    issue_number: int | None = None,
    event: str | None = None,
    limit: int = 100,
) -> dict:
    """Newest-first events matching the filters, with the full matching total."""
    if event is not None:
        _one_of("event", event, EVENTS)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive int")
    clauses, params = [], []
    repo = repo.lower() if repo else repo  # stored lowercased by append_event
    for col, val in (("repo", repo), ("issue_number", issue_number), ("event", event)):
        if val is not None:
            clauses.append(f"{col} = ?")
            params.append(val)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = await db.execute(f"SELECT COUNT(*) FROM board_events {where}", params)
    total = (await cur.fetchone())[0]
    cur = await db.execute(
        f"SELECT * FROM board_events {where} ORDER BY id DESC LIMIT {int(limit)}", params
    )
    items = []
    for item in await _all(cur):
        if item.get("detail"):
            try:
                item["detail"] = json.loads(item["detail"])
            except ValueError:
                item["detail"] = {"unparseable": True}
        items.append(item)
    return {"items": items, "listed": len(items), "total": total}


# ─── retention ──────────────────────────────────────────────────────────────


async def prune(
    db: aiosqlite.Connection,
    *,
    now: str,
    question_days: int = 90,
    event_days: int = 180,
) -> dict:
    """Delete CLOSED questions (and their edges) older than ``question_days``
    and events older than ``event_days``. Unverified questions and pointers are
    never pruned — they are open work and durable fact respectively. Returns the
    real per-table deletion counts."""
    for name, days in (("question_days", question_days), ("event_days", event_days)):
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError(f"{name} must be a positive int")
    now_dt = datetime.fromisoformat(now)
    q_cutoff = (now_dt - timedelta(days=question_days)).isoformat()
    e_cutoff = (now_dt - timedelta(days=event_days)).isoformat()
    doomed = (
        "SELECT id FROM open_questions WHERE status IN ('resolved','dropped') AND closed_at < ?"
    )
    async with _write_unit(db, "prune"):
        cur = await db.execute(
            f"DELETE FROM open_question_blocks WHERE question_id IN ({doomed})", (q_cutoff,)
        )
        blocks = cur.rowcount
        cur = await db.execute(
            f"DELETE FROM open_questions WHERE id IN ({doomed})", (q_cutoff,)
        )
        questions = cur.rowcount
        cur = await db.execute("DELETE FROM board_events WHERE created_at < ?", (e_cutoff,))
        events = cur.rowcount
    return {"questions": questions, "question_blocks": blocks, "events": events}
