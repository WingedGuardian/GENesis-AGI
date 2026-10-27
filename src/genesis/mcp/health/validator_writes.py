"""Validator writers own separate admitted handles, never the health transaction."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import aiosqlite
from fastmcp.exceptions import ToolError
from pydantic import StrictInt

from genesis.db.connection import connect_aiosqlite_rw
from genesis.db.crud import follow_ups
from genesis.mcp.health import mcp
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)


async def finish_connection(db):
    """Finish this same close under repeated cancellation, then propagate it."""
    cleanup = tracked_task(db.close(), name="validator-writer-close")
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


async def owned_call(shared, operation):
    """A returned operation result survives a later close/reporting failure."""
    if shared is None or not getattr(shared, "_db_path", None):
        raise ToolError("Service database path is unavailable")
    db = await connect_aiosqlite_rw(shared._db_path, existing_only=True, timeout=10)
    result = None
    try:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000")
        result = await operation(db)
    except BaseException:
        logger.warning("Validator operation outcome unconfirmed; re-read before retrying")
        raise
    finally:
        try:
            await finish_connection(db)
        except Exception as exc:
            if result is None:
                raise
            logger.warning("Validator operation completed; cleanup failed (%s)", type(exc).__name__)
            result = result | {
                "warning": "Operation completed; cleanup failed. Read state before retrying."
            }
    return result


async def private_create(shared, **fields):
    async def write(db):
        fid = await follow_ups.create_private(db, **fields)
        try:
            row = await follow_ups.get_by_id(db, fid)
            return {"id": fid, "status": row["status"], "item": row}
        except Exception as exc:
            logger.warning("Private create/replay completed; readback failed (%s)", type(exc).__name__)
            return {
                "id": fid,
                "warning": "Create or replay completed; readback unavailable. Re-read current state.",
            }

    return await owned_call(shared, write)


async def private_update(shared, follow_up_id, **fields):
    async def write(db):
        matches, outcome = await follow_ups.resolve_id(db, follow_up_id)
        if len(matches) != 1:
            raise ToolError("Private follow-up id is " + outcome)
        changed = await follow_ups.update_private(db, matches[0], **fields)
        return {"id": matches[0], "updated": changed}

    return await owned_call(shared, write)


async def private_list(db, *, status_filter, limit, include_tabled):
    if db is None:
        raise ToolError("Service database is unavailable")
    if status_filter and status_filter not in follow_ups._PRIVATE_STATUSES:
        raise ToolError("Invalid private follow-up status filter")
    limit = max(1, min(limit, 200))
    where = follow_ups.PRIVATE_FOLLOW_UP_SQL
    params = []
    if status_filter:
        where += " AND status = ?"
        params.append(status_filter)
    if not include_tabled:
        where += " AND kind = 'follow_up'"
    async with db.execute(
        f"SELECT * FROM follow_ups WHERE {where} ORDER BY created_at DESC, id LIMIT ?",  # noqa: S608
        (*params, limit),
    ) as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]
    async with db.execute(
        f"SELECT status, COUNT(*) FROM follow_ups WHERE {where} GROUP BY status",  # noqa: S608 -- closed predicate, values bound
        params,
    ) as cursor:
        counts = dict(await cursor.fetchall())
    return {
        "items": rows,
        "counts": counts,
        "limit": limit,
        "returned": len(rows),
        "limit_hit": len(rows) == limit,
        "counts_scope": "same eligibility and filters; separate read",
    }


@mcp.tool(task=False)
async def pr_verification_close(
    evidence_json: str,
    pr: StrictInt,
    park: bool = False,
    note: str | None = None,
    verdict: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Derive a verification result; server calibration policy can force preview.

    Preview never changes the obligation. After an uncertain real response,
    re-read evidence/attempt_count before retrying: attempts are not idempotent.
    """
    from genesis.db.crud import pr_verifications as crud
    from genesis.mcp.health.follow_up_tools import _get_db
    from genesis.session_awareness.codex_validator_config import dry_run_only
    from genesis.session_awareness.pr_evidence import (
        MAX_EVIDENCE_BYTES,
        Refusal,
        decide,
        parse_evidence,
        reason_for,
    )
    from genesis.session_awareness.pr_recording import _no_duplicates, attempt_note, resolve_repo

    if len(evidence_json.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise ToolError("Evidence exceeds the byte cap; refused rather than truncated")
    doc = parse_evidence(json.loads(evidence_json, object_pairs_hook=_no_duplicates))
    if type(pr) is not int or pr != doc.pr:
        raise ToolError("Independent pr must match the evidence document")
    decision = decide(doc, park=park, asserted=verdict, note=note)
    if isinstance(decision, Refusal):
        raise ToolError(decision.message)
    payload = json.dumps(doc.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise ToolError("Canonical evidence exceeds the byte cap")
    forced = dry_run_only()
    preview = dry_run or forced
    shared = _get_db()
    if shared is None:
        raise ToolError("Service database is unavailable")

    async def record(db):
        if not await crud.tables_available(db) or not await crud._verdict_columns_available(db):
            raise ToolError("Verification schema is unavailable")
        target = resolve_repo(await crud.open_repos_for_pr(db, pr_number=pr), pr, doc.repo)
        result = {
            "repo": target,
            "pr": pr,
            "verdict": decision.verdict,
            "would_close": decision.closing,
            "dry_run": preview,
            "server_dry_run_only": forced,
            "recorded": False,
        }
        if preview:
            return result
        now = datetime.now(UTC).isoformat()
        if decision.closing:
            changed = await crud.close_verification(
                db,
                repo=target,
                pr_number=pr,
                verdict=decision.verdict,
                reason=reason_for(doc, decision),
                evidence=payload,
                now=now,
            )
        else:
            changed = (
                await crud.record_attempt(
                    db,
                    repo=target,
                    pr_number=pr,
                    verdict=decision.verdict,
                    note=attempt_note(decision, note),
                    evidence=payload,
                    now=now,
                )
                == "recorded"
            )
        if not changed:
            raise ToolError("Obligation changed; re-read before deciding whether to retry")
        return result | {"recorded": True}

    return await record(shared) if preview else await owned_call(shared, record)
