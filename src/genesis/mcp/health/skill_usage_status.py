"""skill_usage_status MCP tool — which skills actually fire (issue #3197).

A thin wrapper over ``genesis.learning.skills.usage``, which does the scan; the
same report comes from ``python -m genesis.learning.skills.usage``. A separate
tool rather than a ``loop_closure_status`` section because it reads gigabytes of
transcripts (minutes on a cold cache), while that surface is a cheap DB read.

Each transcript is scanned in its own thread hop, so the server loop stays
responsive and a cancelled call stops between files. Progress is reported at
most every ``_PROGRESS_EVERY_S`` seconds: Claude Code aborts a stdio tool call
that sends nothing for ``CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`` (default 30 min),
and a progress notification resets that clock (measured on CC 2.1.280; see
``browser.py``). ``MCP_TIMEOUT`` does not bound a tool call.

One scan at a time per server process: a second call while one runs returns
``scan_in_progress`` instead of starting a parallel multi-gigabyte read.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from fastmcp import Context

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Same throttle and send bound as browser_fill's progress reporter, for the
# same reason: a report awaits the client transport, so it gets its own bound.
_PROGRESS_EVERY_S = 5.0
_PROGRESS_SEND_S = 1.0

_scan_lock = asyncio.Lock()


async def _in_thread(fn, *args):
    """``fn(*args)`` in a worker thread, returning only once that thread is done.

    Cancelling ``asyncio.to_thread`` does not stop its thread, so a plain await
    would release ``_scan_lock`` while the worker is still reading and let a
    second scan start beside it. On cancel, wait for the worker to end (further
    cancels included), then re-raise the cancellation, never the worker's result.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "skill_usage_status: worker failed after cancel", exc_info=task.exception()
            )
        raise


async def _impl_skill_usage_status(days: float = 14.0, ctx: Context | None = None) -> dict:
    from genesis.learning.skills import usage

    try:
        days = usage.validate_days(days)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if _scan_lock.locked():
        return {
            "status": "scan_in_progress",
            "message": "another skill_usage_status scan is running",
        }

    async with _scan_lock:
        started = time.monotonic()
        until = datetime.now(UTC)
        since = until - timedelta(days=days)

        injected, db_source = None, None
        import genesis.mcp.health_mcp as health_mcp_mod

        service = health_mcp_mod._service
        if service is not None and getattr(service, "_db", None) is not None:
            db_source = "genesis-server database connection"
            try:
                injected = await usage.read_injected(service._db, since, until)
            except Exception:
                logger.warning("skill_usage_status: cc_sessions read failed", exc_info=True)

        population = await _in_thread(usage._default_population)
        scanner = usage.Scanner(since, until)
        files = await _in_thread(
            lambda: list(
                usage.iter_transcript_files(usage.claude_root() / "projects", since, scanner.issues)
            )
        )
        last_sent = time.monotonic()
        report_failed = False
        for done, (path, is_subagent) in enumerate(files, 1):
            await _in_thread(scanner.scan_file, path, is_subagent)
            if ctx is None or time.monotonic() - last_sent < _PROGRESS_EVERY_S:
                continue
            last_sent = time.monotonic()
            try:
                await asyncio.wait_for(
                    ctx.report_progress(
                        done, len(files), message=f"{done}/{len(files)} transcripts"
                    ),
                    timeout=_PROGRESS_SEND_S,
                )
            except Exception:  # best effort: a lost report must not fail the scan
                if not report_failed:
                    logger.warning("skill_usage_status progress report failed", exc_info=True)
                report_failed = True

        return usage.build_report(
            scanner,
            population,
            injected,
            duration_s=time.monotonic() - started,
            db_source=db_source,
        )


@mcp.tool()
async def skill_usage_status(days: float = 14.0, ctx: Context | None = None) -> dict:
    """Which skills actually fire? Per-skill loads, slash loads, SKILL.md reads,
    skill-injection nudges, dispatched injections and nudge-to-use conversion,
    with a state: active / zombie (nudged, never used) / dead (nudgeable,
    silent) / not_nudgeable (silent, the hook cannot offer it).

    Counts are split interactive vs headless (dispatched, SDK and test runs).
    The window and the session denominator are in the output, so a zero means
    "none found in the scanned sessions". Read-only. Scans Claude Code
    transcripts, so a cold 14-day call can take minutes; progress is reported
    while it runs. The same report: ``python -m genesis.learning.skills.usage``.
    """
    return await _impl_skill_usage_status(days, ctx)
