"""Peer branch of the existing runner; activation belongs to the coordinator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from genesis.cc.types import CCInvocation, SessionType
from genesis.peers.session import PeerSessionBinding
from genesis.util.inflight import close_unit, open_unit, within
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)


@dataclass
class PeerRunState:
    binding: PeerSessionBinding
    started: bool = False
    cancelled: bool = False
    acquired: bool = False


async def _ceiling(runtime) -> int:
    from genesis.autonomy.types import AutonomyCategory
    from genesis.db.crud.autonomy import bayesian_posterior

    manager = getattr(runtime, "_autonomy_manager", None)
    try:
        if manager is None:
            raise RuntimeError
        state = await manager.get_state(AutonomyCategory.BACKGROUND_COGNITIVE.value)
        if state is None or int(state.current_level) not in (1, 2, 3, 4):
            raise RuntimeError
        if (
            state.total_corrections > 3
            and bayesian_posterior(state.total_successes, state.total_corrections) < 0.15
        ):
            raise RuntimeError
        return min(3, int(state.current_level))
    except Exception:
        raise RuntimeError("Peer autonomy readiness is unavailable") from None


def _lifecycle(runner):
    lifecycle = getattr(runner._rt, "_peer_session_lifecycle", None)
    if lifecycle is None or not all(
        callable(getattr(lifecycle, name, None))
        for name in ("authorize", "begin", "drain", "finish")
    ):
        raise RuntimeError("Peer session coordinator is unavailable")
    if getattr(runner, "_peer_cleanup_holds", {}):
        raise RuntimeError("Peer cleanup requires reconciliation")
    return lifecycle


async def spawn_peer(runner, request) -> str:
    request.__post_init__()
    binding = request.peer_binding
    if getattr(runner._rt, "_db", None) is None:
        raise RuntimeError("Peer session storage is unavailable")
    if not isinstance(binding, PeerSessionBinding):
        raise ValueError("Invalid internal peer session binding")
    lifecycle = _lifecycle(runner)
    binding.segment.validate_facade_config(binding.facade_config)
    ceiling = await _ceiling(runner._rt)
    if await lifecycle.authorize(binding) is not None:
        raise RuntimeError("Peer authorization was not confirmed")
    _lifecycle(runner)
    state = PeerRunState(binding)
    try:
        session, _ = await _settle(
            runner._session_manager.create_background(
                session_type=SessionType.BACKGROUND_TASK,
                model=request.model,
                effort=request.effort,
                source_tag="peer_api",
                dispatch_mode="peer",
                profile="peer",
                origin="external_untrusted",
                initial_metadata=binding.metadata(ceiling),
            ),
            state,
        )
    except BaseException:
        # A failed commit/read can have an unknown durable creation outcome.
        # Keep its binding until the coordinator reconciles the lineage.
        await _abandon(runner, request, lifecycle, state, ceiling, None)
        if state.cancelled:
            raise asyncio.CancelledError from None
        raise RuntimeError("Peer session creation requires reconciliation") from None
    session_id = session["id"]
    unit = None
    coroutine = None
    try:
        unit = open_unit("direct_session", "peer_api", item_id=session_id)
        coroutine = _run_peer(runner, request, lifecycle, ceiling, session_id, unit, state)
        task = tracked_task(coroutine, name=f"peer-session-{session_id[:8]}")
    except BaseException:
        if coroutine is not None:
            coroutine.close()
        if unit is not None:
            close_unit(unit)
        await _abandon(runner, request, lifecycle, state, ceiling, session_id)
        raise
    runner._peer_runs[session_id] = state
    runner._active[session_id] = task
    task.add_done_callback(lambda _t: runner._peer_runs.pop(session_id, None))
    task.add_done_callback(lambda _t: runner._active.pop(session_id, None))
    task.add_done_callback(lambda _t: close_unit(unit))
    if state.cancelled:
        with contextlib.suppress(asyncio.CancelledError):
            await _settle(task, state)
        raise asyncio.CancelledError
    return session_id


async def _abandon(runner, request, lifecycle, state, ceiling, session_id):
    try:
        clean, _ = await _settle(_drain(state.binding, lifecycle), state)
    except BaseException:
        clean = False
    if session_id is None or not clean:
        runner._peer_cleanup_holds[session_id or state.binding.segment.segment_id] = state
    if session_id is not None:
        await _settle(
            _finalize(
                runner, request, lifecycle, session_id, None, [], clean, state, ceiling, False, 0
            ),
            state,
        )


async def _settle(coroutine, state=None):
    """Retain effect ownership through repeated cancellation; report its delivery."""
    task = asyncio.ensure_future(coroutine)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if state is not None:
                state.cancelled = True
    return task.result(), cancelled


async def _drain(binding, lifecycle) -> bool:
    results = await asyncio.gather(
        binding.segment.stop_and_drain(),
        lifecycle.drain(binding),
        return_exceptions=True,
    )
    return all(value is None for value in results)


async def _run_peer(runner, request, lifecycle, ceiling, session_id, unit, state):
    state.started = True
    binding = request.peer_binding
    output = None
    telemetry = []
    acquired = cancelled = expired = False
    started = time.monotonic()

    async def on_event(event):
        if (
            event.event_type == "tool_use"
            and event.tool_name in binding.segment.tools
            and len(telemetry) < 100
        ):
            telemetry.append({"name": event.tool_name})

    with within(unit):
        try:
            if state.cancelled:
                raise asyncio.CancelledError
            _lifecycle(runner)
            remaining = min(request.timeout_s, binding.segment.deadline_at - time.time())
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await runner._semaphore.acquire()
                acquired = state.acquired = True
                _lifecycle(runner)
                if await lifecycle.begin(binding, session_id, ceiling) is not None:
                    raise RuntimeError("Peer segment start was not confirmed")
                _lifecycle(runner)
                if state.cancelled:
                    raise asyncio.CancelledError
                invocation = CCInvocation(
                    prompt=request.prompt,
                    model=request.model,
                    effort=request.effort,
                    system_prompt=runner._config_builder._load_identity_block(),
                    mcp_config=binding.facade_config,
                    working_dir=binding.working_dir,
                    timeout_s=request.timeout_s,
                    origin="external_untrusted",
                    peer_segment=binding.segment,
                    roster_eligible=True,
                )
                output = await runner._invoker.run_streaming(invocation, on_event=on_event)
        except asyncio.CancelledError:
            cancelled = state.cancelled = True
        except TimeoutError:
            expired = True
        except Exception:
            logger.warning("Peer segment execution failed")

        try:
            clean, interrupted = await _settle(_drain(binding, lifecycle), state)
            cancelled = cancelled or interrupted
        except BaseException:
            clean = False
        if clean and acquired:
            runner._semaphore.release()
            state.acquired = False
        if not clean:
            # Retain any capacity token and disable peers until unit8 confirms
            # both drains; even a never-started segment can own a broker lease.
            runner._peer_cleanup_holds[session_id] = state
        result, interrupted = await _settle(
            _finalize(
                runner,
                request,
                lifecycle,
                session_id,
                output,
                telemetry,
                clean,
                state,
                ceiling,
                expired,
                time.monotonic() - started,
            ),
            state,
        )
        if state.cancelled or interrupted:
            raise asyncio.CancelledError
        return result


async def _finalize(
    runner,
    request,
    lifecycle,
    session_id,
    output,
    telemetry,
    clean,
    state,
    ceiling,
    expired,
    elapsed,
):
    from genesis.cc.direct_session import DirectSessionResult
    from genesis.security.output_scanner import scan_outbound
    from genesis.util.atomic import atomic_write_text

    binding = request.peer_binding
    text = output.text if output is not None else ""
    success = (
        output is not None and not output.is_error and clean and not state.cancelled and not expired
    )
    error = None if success else "Peer segment did not complete"
    if text and (
        not scan_outbound(text).safe or binding.working_dir in text or binding.facade_config in text
    ):
        text, success, error = "", False, "Peer result withheld"
    artifact = None
    if text or success:
        try:
            directory = Path(binding.working_dir) / ".peer-results"
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()
            ):
                raise RuntimeError("Peer result directory is not private")
            path = directory / f"bg-session-{session_id}.md"
            await asyncio.to_thread(atomic_write_text, path, text)
            artifact = str(path)
        except Exception:
            text, success, error = "", False, "Peer result publication failed"
    if state.cancelled:
        success, error = False, "Peer segment did not complete"
    result = DirectSessionResult(
        session_id=session_id,
        success=success,
        output_text=text,
        error=error,
        duration_s=round(elapsed, 1),
        tools_called=telemetry,
        cost_usd=output.cost_usd if output is not None else 0,
        input_tokens=output.input_tokens if output is not None else 0,
        output_tokens=output.output_tokens if output is not None else 0,
    )
    metadata = binding.metadata(ceiling)
    metadata.update(
        transcript_path="",
        result_artifact_path=artifact,
        cleanup_confirmed=clean,
        peer_cancelled=state.cancelled,
        peer_expired=expired,
    )
    try:
        await runner._store_result(session_id, request, result, extra_metadata=metadata)
        if state.cancelled:
            result.success, result.error = False, "Peer segment did not complete"
            await runner._store_result(
                session_id, request, result, extra_metadata={"peer_cancelled": True}
            )
        if result.success:
            await runner._session_manager.complete(
                session_id,
                cost_usd=result.cost_usd,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        else:
            await runner._session_manager.fail(session_id, reason="peer_segment_failed")
    except Exception:
        result.success = False
        result.error = "Peer result recording failed"
        logger.warning("Peer result recording failed")
    if state.cancelled and result.success:
        result.success, result.error = False, "Peer segment did not complete"
        await runner._store_result(
            session_id, request, result, extra_metadata={"peer_cancelled": True}
        )
        await runner._session_manager.fail(session_id, reason="peer_segment_cancelled")
    await lifecycle.finish(
        binding,
        session_id,
        {
            "success": result.success,
            "error": result.error,
            "artifact_path": artifact,
            "tools_summary": runner._summarize_tools(telemetry),
            "cleanup_confirmed": clean,
            "cancelled": state.cancelled,
            "expired": expired,
            "duration_s": result.duration_s,
        },
    )
    return result
