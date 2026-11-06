"""Rechecks owed by a closed verification; no operation reopens its row."""

from __future__ import annotations

import json

import aiosqlite
from pydantic import ValidationError

RECHECK_RESULTS = ("done", "failed")
_FIELDS = ("trigger", "recheck", "detail")
_MAX_INDEX = 2**31 - 1  # SQLite JSON indices beyond this can wrap onto another entry.


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def normalize_rechecks(items: list[dict] | None) -> str | None:
    """Normalize fresh entries; callers cannot inject result history or completion."""
    from genesis.session_awareness.pr_evidence import EventGatedGap

    if items is None:
        return None
    if not isinstance(items, list):
        raise ValueError("a recheck needs a list of entries")
    out = []
    for item in items:
        fields = {k: item.get(k) if isinstance(item, dict) else None for k in _FIELDS}
        try:
            gap = EventGatedGap.model_validate({"kind": "event_gated", **fields})
        except ValidationError as exc:
            raise ValueError(f"a recheck needs valid non-empty {_FIELDS}: {exc}") from None
        out.append({**{k: getattr(gap, k) for k in _FIELDS}, "results": [], "done_at": None})
    return json.dumps(out, ensure_ascii=False) if out else None


def rechecks_from_evidence(evidence: str | None) -> list[dict] | None:
    """Capture typed gaps from existing close callers, including clients predating us.

    Legacy free text is still accepted as evidence; only structured event_gated
    gaps declare rechecks. Malformed declared entries are refused by normalization.
    """
    try:
        doc = json.loads(evidence) if evidence else None
    except (TypeError, ValueError):
        return None
    gaps = doc.get("gaps") if isinstance(doc, dict) else None
    if not isinstance(gaps, list):
        return None
    return [g for g in gaps if isinstance(g, dict) and g.get("kind") == "event_gated"]


def _parse_rechecks(raw: str) -> tuple[list[dict] | None, str]:
    """One stored-shape check for due, append, dry run and retention."""
    from genesis.session_awareness.pr_recording import _no_duplicates

    try:
        items = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (TypeError, ValueError, RecursionError):
        return None, "not-json"
    if not isinstance(items, list):
        return None, "bad-entry"
    try:
        normalized = json.loads(normalize_rechecks(items) or "[]")
    except (ValueError, RecursionError):
        return None, "bad-entry"
    for item, fresh in zip(items, normalized, strict=True):
        # Use the model's stripped fields: a padded date is still a date,
        # including when it came from the migration rather than a current writer.
        item.update({k: fresh[k] for k in _FIELDS})
        history = item.get("results")
        if not isinstance(history, list) or set(item) != {*_FIELDS, "results", "done_at"}:
            return None, "bad-entry"
        for result in history:
            if (
                not isinstance(result, dict)
                or set(result) != {"at", "result", "note"}
                or result.get("result") not in RECHECK_RESULTS
                or not _text(result.get("at"))
                or not _text(result.get("note"))
            ):
                return None, "bad-entry"
        done_at = item["done_at"]
        if any(r["result"] == "done" for r in history[:-1]):
            return None, "bad-entry"
        finished = bool(history and history[-1]["result"] == "done")
        if (done_at is not None) != finished or (
            finished and (not _text(done_at) or done_at != history[-1]["at"])
        ):
            return None, "bad-entry"
    return items, "ok"


async def rechecks_column_available(db: aiosqlite.Connection) -> bool:
    """Uncached so pre-migration clients recover as soon as the column arrives."""
    cursor = await db.execute(
        "SELECT 1 FROM pragma_table_info('pr_verifications') WHERE name = 'rechecks'"
    )
    return await cursor.fetchone() is not None


async def list_rechecks_due(db: aiosqlite.Connection, *, today: str) -> list[dict]:
    """All unfinished due dates and named events, oldest close first, without a cap.

    Named events require validator judgment. An unreadable row is surfaced once
    rather than hidden behind its malformed entry or history.
    """
    from genesis.session_awareness.pr_evidence import TRIGGER_DATE

    if not await rechecks_column_available(db):
        return []
    cursor = await db.execute(
        "SELECT repo, pr_number, pr_title, closed_at, rechecks FROM pr_verifications "
        "WHERE status = 'closed' AND rechecks IS NOT NULL "
        "ORDER BY closed_at ASC, repo ASC, pr_number ASC"
    )
    out = []
    for row in await cursor.fetchall():
        base = dict(zip(("repo", "pr_number", "pr_title", "closed_at"), row[:4], strict=True))
        items, state = _parse_rechecks(row[4])
        if state != "ok":
            out.append({**base, "index": None, "malformed": True})
            continue
        for index, item in enumerate(items):
            if item["done_at"] is not None:
                continue
            trigger = item["trigger"]
            if TRIGGER_DATE.fullmatch(trigger) and trigger > today:
                continue
            results = item["results"]
            out.append(
                {
                    **base,
                    "index": index,
                    "malformed": False,
                    **{k: item[k] for k in _FIELDS},
                    "attempts": len(results),
                    "latest": results[-1] if results else None,
                }
            )
    return out


async def _target(db, *, repo: str, pr_number: int, index: int) -> tuple[str, str | None]:
    if type(index) is not int or index < 0:
        raise ValueError(f"index must be a non-negative integer, got {index!r}")
    if index > _MAX_INDEX:
        return "out-of-range", None
    if not await rechecks_column_available(db):
        return "unavailable", None
    cursor = await db.execute(
        "SELECT rechecks FROM pr_verifications WHERE repo = ? AND pr_number = ? "
        "AND status = 'closed'",
        (repo, pr_number),
    )
    row = await cursor.fetchone()
    if row is None:
        return "missing", None
    raw = row[0]
    if raw is None:
        return "no-rechecks", None
    items, state = _parse_rechecks(raw)
    if state != "ok":
        return state, raw
    if not items:
        return "no-rechecks", raw
    if index >= len(items):
        return "out-of-range", raw
    return ("already-done" if items[index]["done_at"] is not None else "ok"), raw


async def recheck_state(db: aiosqlite.Connection, *, repo: str, pr_number: int, index: int) -> str:
    """Refusal cause, or ok, without writing; shared with the real append."""
    state, _ = await _target(db, repo=repo, pr_number=pr_number, index=index)
    return state


async def append_recheck_result(
    db: aiosqlite.Connection,
    *,
    repo: str,
    pr_number: int,
    index: int,
    result: str,
    note: str,
    now: str,
) -> str:
    """Append once to the exact validated state; a contending loser returns raced.

    Success is one atomic UPDATE of rechecks alone. Original verdict, evidence,
    attempt count and closure metadata remain intact. A done entry is terminal.
    """
    if result not in RECHECK_RESULTS:
        raise ValueError(f"result must be one of {RECHECK_RESULTS}, got {result!r}")
    if not _text(note) or not _text(now):
        raise ValueError("a recheck result requires a non-empty note and timestamp")
    state, raw = await _target(db, repo=repo, pr_number=pr_number, index=index)
    if state != "ok":
        return state
    entry = json.dumps({"at": now, "result": result, "note": note.strip()}, ensure_ascii=False)
    at = f"$[{index}]"
    value = "json_insert(rechecks, ?, json(?))"
    params = [f"{at}.results[#]", entry]
    if result == "done":
        value = f"json_set({value}, ?, ?)"
        params += [f"{at}.done_at", now]
    cursor = await db.execute(
        f"UPDATE pr_verifications SET rechecks = {value} "
        "WHERE repo = ? AND pr_number = ? AND status = 'closed' AND rechecks IS ?",
        (*params, repo, pr_number, raw),
    )
    await db.commit()
    if cursor.rowcount:
        return "recorded"
    state = await recheck_state(db, repo=repo, pr_number=pr_number, index=index)
    return "raced" if state == "ok" else state


async def prune_recheck_free_closed(db: aiosqlite.Connection, *, cutoff: str) -> int:
    """Delete only fully understood, completed rows, binding to the value checked."""
    cursor = await db.execute(
        "SELECT repo, pr_number, rechecks FROM pr_verifications "
        "WHERE status = 'closed' AND closed_at < ?",
        (cutoff,),
    )
    eligible = []
    for repo, pr, raw in await cursor.fetchall():
        items, state = ([], "ok") if raw is None else _parse_rechecks(raw)
        if state == "ok" and all(i["done_at"] is not None for i in items):
            eligible.append((repo, pr, cutoff, raw))
    cursor = await db.executemany(
        "DELETE FROM pr_verifications WHERE repo = ? AND pr_number = ? "
        "AND status = 'closed' AND closed_at < ? AND rechecks IS ?",
        eligible,
    )
    await db.commit()
    return cursor.rowcount
