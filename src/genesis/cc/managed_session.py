"""Prepared native receipts, without a second execution queue or task store."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.db.crud import cc_sessions
from genesis.util.atomic import atomic_write_text
from genesis.util.execution_privacy import private_execution

logger = logging.getLogger(__name__)

# Terminal native states, written only by _finalize, mapped to the row status
# its complete()/fail() call would have set.
_COMMITTED_STATUS = {"succeeded": "completed", "failed": "failed", "cancelled": "failed"}

# How long a held uncertain receipt sleeps between (write-free) wakeups.
_HOLD_PARK_S = 3600.0


def fingerprint(request) -> str:
    return hashlib.sha256(json.dumps(asdict(request), sort_keys=True).encode()).hexdigest()


def prepared_metadata(request) -> dict:
    """Metadata for the caller's atomic cc_sessions.create(commit=False)."""
    if not request.managed:
        raise ValueError("A managed request is required")
    return {
        "managed_fingerprint": fingerprint(request),
        "native_state": "pending",
        "profile": request.profile,
        "caller_context": request.caller_context,
        "deadline_at": (datetime.now(UTC) + timedelta(seconds=request.timeout_s)).isoformat(),
        "result_committed": False,
    }


def metadata(row) -> dict:
    try:
        value = json.loads(row.get("metadata") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


async def update_state(db, sid: str, state: str, **fields) -> None:
    if not await cc_sessions.merge_metadata(db, sid, {"native_state": state, **fields}):
        raise ValueError("Prepared receipt is unavailable")


async def hold_uncertain(db, sid: str) -> None:
    """Never return: no safe process identity remains after uncertain cleanup.

    The caller keeps everything it holds (receipt, runner slot) until runtime
    teardown; an owner restart reconciles the receipt as outcome_unknown. Even
    repeated cancellation cannot turn uncertainty into confirmation. The state
    is written until one write succeeds, then held without further writes.
    """
    delay = 1.0
    recorded = False
    while True:
        try:
            if not recorded:
                # Persistence refusal also cannot establish cleanup: retry.
                with contextlib.suppress(Exception):
                    await update_state(
                        db,
                        sid,
                        "cancelling",
                        error="cleanup_unconfirmed",
                        cleanup_unconfirmed=True,
                    )
                    recorded = True
                    # This module's logger is outside the private-execution
                    # filter, so the hold is visible; the line carries no content.
                    logger.warning(
                        "Managed receipt %s held: process cleanup unconfirmed; "
                        "its runner slot is kept until restart",
                        sid,
                    )
            if recorded:
                await asyncio.sleep(_HOLD_PARK_S)
            else:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        except asyncio.CancelledError:
            continue


async def reconcile_orphans(db) -> int:
    """Boot-only reconciliation, before exposing a newly created runner.

    A prior process may have executed effects. Never replay its receipt or
    report confirmed cancellation merely because the new registry is empty.
    Only prepared receipts (rows carrying a managed fingerprint) are touched.
    A receipt whose finalization already committed its terminal outcome lost
    only the status write, so it is finished with that outcome, not overwritten.
    """
    orphaned = [
        row
        for row in await cc_sessions.query_active(db)
        if row.get("source_tag") == "agent_api" and "managed_fingerprint" in metadata(row)
    ]
    for row in orphaned:
        meta = metadata(row)
        finished = _COMMITTED_STATUS.get(meta.get("native_state"))
        if finished is not None:
            usage = meta.get("usage")
            if isinstance(usage, dict):
                with contextlib.suppress(TypeError, ValueError, OverflowError):
                    await cc_sessions.set_turn_totals(
                        db,
                        row["id"],
                        cost_usd=float(usage.get("cost_usd") or 0.0),
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                    )
            await cc_sessions.update_status(db, row["id"], status=finished)
            continue
        await update_state(db, row["id"], "failed", error="outcome_unknown")
        await cc_sessions.update_status(db, row["id"], status="failed")
    return len(orphaned)


async def run_managed(runner, request, sid: str, cancel: asyncio.Event):
    """Strongly own execution and finalization until effects are drained.

    Cancellation is a signal, never abandonment of the supervisor. If result
    persistence fails, keep capacity reserved until persistence succeeds.
    """
    from genesis.cc.direct_session import DirectSessionResult

    db = runner._session_manager.db
    worker = None
    state, error = "failed", "execution_failed"
    result = DirectSessionResult(session_id=sid)
    with private_execution():
        try:
            row = await cc_sessions.get_by_id(db, sid)
            deadline = datetime.fromisoformat(metadata(row)["deadline_at"])
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if cancel.is_set():
                state, error = "cancelled", "cancelled"
            elif remaining <= 0:
                error = "deadline_exceeded"
            else:
                worker = asyncio.create_task(_execute(runner, request, sid))
                signal = asyncio.create_task(cancel.wait())
                try:
                    done, _ = await asyncio.wait(
                        (worker, signal),
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if worker in done:
                        result = worker.result()
                        state = "succeeded" if result.success else "failed"
                        error = None if result.success else "execution_failed"
                    else:
                        state = "cancelled" if cancel.is_set() else "failed"
                        error = "cancelled" if cancel.is_set() else "deadline_exceeded"
                        worker.cancel()
                        # A held uncertain receipt keeps its classification:
                        # cancelling it does not make its child go away. The
                        # check and the write are one statement, so a hold
                        # recorded concurrently is never overwritten.
                        if (
                            not await cc_sessions.merge_metadata(
                                db,
                                sid,
                                {"native_state": "cancelling", "error": error},
                                unless_true="cleanup_unconfirmed",
                            )
                            and await cc_sessions.get_by_id(db, sid) is None
                        ):
                            raise ValueError("Prepared receipt is unavailable")
                finally:
                    signal.cancel()
                    await asyncio.gather(signal, return_exceptions=True)
        except asyncio.CancelledError:
            state, error = "cancelled", "cancelled"
            if worker is not None:
                worker.cancel()
        except Exception:
            # Never propagate private provider/hook details to tracked_task.
            state, error = "failed", "execution_failed"
        finally:
            if worker is not None:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # Retrieve all exceptions without printing them.
                if not worker.cancelled() and worker.exception() is None:
                    result = worker.result()
            result.success = state == "succeeded"
            result.error = error
            # Cancellation cannot interrupt the receipt/artifact commit.
            finalizer = asyncio.create_task(_finalize(runner, request, result, state))
            while not finalizer.done():
                try:
                    await asyncio.shield(finalizer)
                except asyncio.CancelledError:
                    continue
            finalizer.result()
            # Capacity is held until the receipt is persisted, not merely
            # until the subprocess exits.
            runner.release_managed_slot(sid)
    return result


async def _execute(runner, request, sid):
    from genesis.cc.direct_session import DirectSessionResult, _bg_session_root, _set_obs_session

    _set_obs_session(sid)
    await runner._check_background(request)
    await runner._session_manager.start_prepared(sid)
    await update_state(runner._session_manager.db, sid, "running")
    start = time.monotonic()
    telemetry = []

    async def on_event(event):
        for name, _input in event.tool_uses():
            telemetry.append({"name": name})

    error = "execution_failed"
    try:
        output = await runner._invoke(request, sid, on_event)
        result = runner._result(sid, output, telemetry, time.monotonic() - start)
        result.success = result.success and output.result_received and not output.bg_truncated
        return result
    except asyncio.CancelledError:
        error = "cancelled"
    except Exception:
        pass  # Classified below; never park/replay or propagate private details.
    finally:
        shutil.rmtree(_bg_session_root(sid), ignore_errors=True)
    return DirectSessionResult(
        session_id=sid,
        error=error,
        tools_called=telemetry,
        duration_s=round(time.monotonic() - start, 1),
    )


async def _finalize(runner, request, result, state):
    path = Path.home() / ".genesis" / "output" / f"bg-session-{result.session_id}.md"
    committed = False
    try:
        atomic_write_text(path, result.output_text, encoding="utf-8")
        committed = True
    except Exception:
        state, result.success, result.error = "failed", False, "result_unavailable"
    while True:
        try:
            await runner._store_result(
                result.session_id,
                request,
                result,
                extra_metadata={
                    "native_state": state,
                    "result_committed": committed,
                    "result_path": str(path) if committed else None,
                    # Readers verify the file against this: later sessions can
                    # write in the same output directory.
                    "result_sha256": hashlib.sha256(result.output_text.encode("utf-8")).hexdigest()
                    if committed
                    else None,
                    "stream_lines_dropped": result.stream_lines_dropped,
                    "result_partial": result.partial,
                    # Written with the terminal state, so a restart before
                    # set_turn_totals below can still restore them.
                    "usage": {
                        "cost_usd": result.cost_usd,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                    },
                },
            )
            # Absolute fresh-turn values: retries of finalization do not add cost twice.
            await cc_sessions.set_turn_totals(
                runner._session_manager.db,
                result.session_id,
                cost_usd=result.cost_usd,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            if result.success:
                await runner._session_manager.complete(
                    result.session_id,
                    cost_usd=result.cost_usd,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
            else:
                await runner._session_manager.fail(result.session_id, reason=result.error)
            from genesis.cc.managed_fallback import record_managed_outcome

            await record_managed_outcome(runner, result.session_id, result)
            return
        except Exception:
            logger.warning("Managed receipt persistence unavailable; capacity retained")
            await asyncio.sleep(1)
