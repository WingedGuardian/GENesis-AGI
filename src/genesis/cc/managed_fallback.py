"""Select before managed work starts; never replay a task on another provider."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from html import escape

from genesis.cc import fallback_recovery, fallback_state, peer_availability, roster
from genesis.cc.exceptions import (
    CCProcessError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCTimeoutError,
)
from genesis.cc.provider_readiness import probe_provider
from genesis.db.connection import get_raw_db
from genesis.db.crud import cc_sessions
from genesis.observability.call_site_recorder import record_last_run


async def database_path(db) -> str:
    """Follow the receipt's database, never the process's default database."""
    rows = await (await db.execute("PRAGMA database_list")).fetchall()
    path = next((row[2] for row in rows if row[1] == "main"), "")
    if not path:
        raise ValueError("Managed provider selection requires a durable receipt")
    return path


async def _observe_home(runner, home, provider, error=None):
    """Forward current-home evidence; callers already classify refusals."""
    if provider != home or roster.active_model() != home:
        return
    observer = getattr(runner._rt, "_cc_invoker", None)
    if observer is not None:
        # The shared notifier understands rate/quota, while this seam also
        # receives already-classified credit refusals from process errors.
        if error is not None and not isinstance(error, (CCRateLimitError, CCQuotaExhaustedError)):
            error = CCQuotaExhaustedError()
        await observer._notify_status_change(error)
    # Health can recover without a successful fallback; another home's banner
    # must survive. Omitting the observer here avoids a second NORMAL callback.
    if error is None and fallback_state.read().original == home:
        await fallback_recovery.note_home_recovery()


async def select_provider(runner, request, sid, invocation):
    if not invocation.roster_eligible and request.roster_model is None:
        return "", invocation  # Explicit routing opt-out remains authoritative.
    from genesis.cc.managed_session import metadata

    config = roster.load_roster()
    home = request.roster_model if request.roster_model is not None else roster.active_model(config)
    # Home/pin errors are fatal; the established roster helper skips unusable
    # backups. Resolve every candidate from this single snapshot.
    routing = dict(anthropic_base_url=None, anthropic_auth_token=None, model_id_override=None)
    routing.update(roster.overrides_for(home, config))
    candidates = [
        (
            home,
            replace(
                invocation,
                roster_eligible=bool(routing["model_id_override"]),
                resume_session_id=None,
                **routing,
            ),
        )
    ]
    if request.roster_model is None:
        candidates.extend(roster.failover_invocations(home, invocation, config))
    path = await database_path(runner._session_manager.db)
    async with get_raw_db(path) as db:
        row = await cc_sessions.get_by_id(db, sid)
    deadline = datetime.fromisoformat(metadata(row)["deadline_at"])
    attempts = []
    for name, selected in candidates:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise CCTimeoutError("Managed provider selection deadline expired")
        attempt = {
            "provider": name,
            "model": selected.model_id_override or str(selected.model),
            "outcome": "checking",
            "at": datetime.now(UTC).isoformat(),
        }
        attempts.append(attempt)
        patch = {
            "provider_attempts": attempts,
            "home_provider": home,
            "fallback_attempted": name != home,
        }
        async with get_raw_db(path) as db:
            if not await cc_sessions.merge_metadata(db, sid, patch):
                raise ValueError("Managed receipt is unavailable")
        try:
            await probe_provider(runner._invoker, selected, timeout_s=min(90, remaining))
        except Exception as exc:
            # Only a genuine provider refusal advances the chain. Cleanup
            # uncertainty, transport, parsing and local faults must propagate.
            if not peer_availability.is_provider_refusal(exc):
                raise
            attempt["outcome"] = "refused"
            await _observe_home(runner, home, name, exc)
            if name != home:
                await asyncio.to_thread(peer_availability.note_failure, name, exc)
        else:
            attempt["outcome"] = "ready"
            patch["execution_provider"] = name
            patch["execution_endpoint"] = roster.endpoint_payload(name, config)
        async with get_raw_db(path) as db:
            if not await cc_sessions.merge_metadata(db, sid, patch):
                raise ValueError("Managed receipt is unavailable")
        if attempt["outcome"] == "ready":
            await _observe_home(runner, home, name)
            return name, selected
    raise CCProcessError("No configured provider established readiness")


async def record_fallback(db, provider, model, *, success):
    """Non-routing last observation; no task/response/provider-error content."""
    try:
        async with get_raw_db(await database_path(db)) as owned:
            await record_last_run(
                owned,
                "cc_fallback",
                provider=provider,
                model_id=model,
                response_text=None,
                success=success,
            )
    except Exception:
        pass  # Telemetry is never permission to lose a task outcome.


async def record_managed_outcome(runner, sid, result):
    """Called only after native classification and terminal receipt commit."""
    from genesis.cc.managed_session import metadata

    try:
        db = runner._session_manager.db
        row = await cc_sessions.get_by_id(db, sid)
        meta = metadata(row)
        selected = meta.get("execution_provider")
        if not meta.get("fallback_attempted") or not selected:
            return
        success = meta.get("native_state") == "succeeded" and row["status"] == "completed"
        last = meta["provider_attempts"][-1]
        await record_fallback(db, selected, result.model_used or last["model"], success=success)
        if success and selected != meta["home_provider"]:
            await asyncio.to_thread(peer_availability.note_success, selected)
            # Like conversation/recovery, keep this read-modify-write on the
            # runtime loop; a worker thread could race another enter or clear.
            if fallback_state.enter(meta["home_provider"], selected, "rate_limit"):
                await fallback_recovery.fire_fallback_alert(
                    topic="cc_fallback_switch",
                    context=(
                        "<b>Fallback</b>\n\nA managed task completed using "
                        f"<b>{escape(selected)}</b> while "
                        f"<b>{escape(meta['home_provider'])}</b> was unavailable. "
                        "Genesis checks the home provider again for each new task."
                    ),
                )
    except Exception:
        pass  # Classified, committed receipts do not depend on diagnostics.
