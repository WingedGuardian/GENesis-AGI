"""FastMCP instrumentation middleware — tracks per-tool call metrics.

Automatically records call counts, latency, and error rates for every MCP
tool invocation.  Uses the "mcp.{server}.{tool}" namespace to avoid
collisions with provider names in the shared ProviderActivityTracker.

Requires FastMCP >= 2.9 (middleware support).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp.server.middleware import Middleware

from genesis.observability.commit_identity import is_stale
from genesis.observability.commit_identity import (
    same_commit as _same_commit,  # noqa: F401 — re-exported for Part A's guard test
)
from genesis.observability.mcp_guarded_tools import GUARDED_MCP_TOOLS
from genesis.observability.provider_activity import ProviderActivityTracker

logger = logging.getLogger(__name__)

# Staleness verdict (`is_stale`) + the prefix-tolerant compare (`same_commit`) live
# in the stdlib-only ``commit_identity`` leaf so the dashboard (Part B) shares the
# SAME verdict without importing ``fastmcp`` via this module. ``_same_commit`` is
# re-exported for Part A's existing guard test.


class InstrumentationMiddleware(Middleware):
    """Records per-tool invocation metrics to ProviderActivityTracker.

    Provider names use the format "mcp.{server_name}.{tool_name}" to keep
    them distinct from LLM provider entries (e.g., "llm.gemini_flash").

    All tracking is fire-and-forget — tracker errors never propagate to
    the MCP tool handler.

    Per-call transaction boundary (WS-15 follow-up): when constructed with the
    server's long-lived ``db`` connection, each tool call commits only after a
    clean return and rolls back otherwise. This releases the read snapshot that a
    read-only tool would otherwise leave open in deferred-isolation mode — which
    pins the SQLite WAL checkpoint and makes later writes fail "database is locked".
    Writes already commit at the CRUD layer before the tool returns, so the commit
    here only closes a *trailing* read txn (it can never discard a tool's own
    write); any non-clean exit — a raised ``Exception``, or a ``BaseException`` no
    clause catches (``asyncio.CancelledError``, ``SystemExit``, …) — rolls back the
    possibly-partial write instead of committing it. The boundary catches nothing;
    every exception propagates unchanged. DB errors in this boundary never
    propagate to the tool handler.
    """

    def __init__(
        self,
        tracker: ProviderActivityTracker,
        server_name: str,
        db: Any = None,
    ) -> None:
        self._tracker = tracker
        self._server = server_name
        self._db = db
        # Latches True once a post-spawn deploy is confirmed (staleness is
        # monotonic — it never reverts). A False verdict is deliberately NOT
        # cached; see _is_stale.
        self._is_stale_latched: bool = False

    async def on_call_tool(self, context, call_next):
        """Wrap tool invocations with timing, error tracking, and a per-call
        commit/rollback boundary (see class docstring)."""
        tool_name = context.message.name
        provider = f"mcp.{self._server}.{tool_name}"
        t0 = time.monotonic()
        # Default False so the finally COMMITS only after a clean return. Any exit
        # that is NOT a normal return leaves this False and rolls back the
        # possibly-partial write instead of committing it: a raised ``Exception``,
        # or a ``BaseException`` no clause catches — ``asyncio.CancelledError``,
        # ``SystemExit``, ``KeyboardInterrupt``, ``GeneratorExit`` (follow-up
        # 3183405d — the old ``except Exception`` let all of those keep success=True
        # and commit a partial). Nothing is caught here, so every exception,
        # cancellation included, propagates unchanged.
        success = False
        try:
            if tool_name in GUARDED_MCP_TOOLS:
                # Raises ToolError (block mode) if this subprocess is running
                # stale code. Inside the try so the finally's rollback releases
                # the read snapshot the staleness check opened on self._db.
                await self._enforce_freshness(tool_name)
            result = await call_next(context)
            success = True
            return result
        finally:
            # Nested try/finally so a BaseException raised by the DB step cannot
            # skip the tracker record below it. Cancellation is the reachable
            # case: `except Exception` does not catch `asyncio.CancelledError`,
            # so before this nesting a cancellation landing in the commit await
            # left the call unrecorded — the tool ran, and the activity tracker
            # never heard about it.
            #
            # What that cancellation does NOT do is lose the write, which is
            # worth stating because the obvious remedy (roll back on cancel) is
            # built on the opposite assumption. aiosqlite QUEUES the operation
            # to its worker thread BEFORE awaiting — `_execute` does
            # `self._tx.put_nowait((future, function))` and only then
            # `await future` — so cancelling the await cancels the WAIT, not the
            # commit. MEASURED against aiosqlite 0.21: a row inserted before a
            # cancelled `commit()` is durable afterwards. Issuing a rollback
            # here would either be a no-op behind the commit already in the
            # queue, or would discard a successful tool call's writes.
            try:
                if self._db is not None:
                    try:
                        if success:
                            await self._db.commit()
                        else:
                            await self._db.rollback()
                    except Exception:
                        logger.warning(
                            "DB %s after %s failed",
                            "commit" if success else "rollback",
                            provider, exc_info=True,
                        )
            finally:
                try:
                    self._tracker.record(
                        provider,
                        latency_ms=(time.monotonic() - t0) * 1000,
                        success=success,
                    )
                except Exception:
                    logger.warning(
                        "Activity tracker record failed for %s",
                        provider, exc_info=True,
                    )

    async def _enforce_freshness(self, tool_name: str) -> None:
        """Block a guarded tool when this subprocess is running stale code.

        Only reached for tools in GUARDED_MCP_TOOLS, so config + staleness are
        read lazily and a fresh (common) call pays nothing beyond the
        set-membership check. Raises fastmcp ``ToolError`` in ``block`` mode
        (surfaced to Claude Code as a clean tool error, session survives); logs
        in ``warn``; no-op in ``off``.
        """
        if not await self._is_stale():
            return
        from genesis.observability.mcp_staleness_guard_config import effective_mode

        mode = effective_mode()
        if mode == "off":
            return
        if mode == "warn":
            logger.warning(
                "MCP staleness guard: '%s' called on a stale subprocess "
                "(warn mode — allowed).",
                tool_name,
            )
            return
        # block (default; any unexpected mode already degraded to block upstream)
        from fastmcp.exceptions import ToolError

        from genesis.observability import mcp_spawn_identity as si

        raise ToolError(
            "This Claude Code session is running stale Genesis code: a deploy "
            "landed after the session started (its MCP server is still on commit "
            f"{(si.spawn_commit() or 'unknown')[:8]}). '{tool_name}' overwrites an "
            "existing procedure by similarity match, and that match logic may have "
            "changed in the deploy — acting on stale logic risks corrupting the "
            "wrong row. Restart this Claude Code session to load the deployed "
            "code, then retry."
        )

    async def _is_stale(self) -> bool:
        """True iff a successful deploy landed AFTER this subprocess started, on a
        DIFFERENT commit than it was spawned at.

        Staleness is MONOTONIC — once a newer deploy exists it never reverts — so
        a True verdict latches permanently (no further DB reads). A False verdict
        is deliberately NOT cached: caching it would open a window where a deploy
        landing just after a not-stale check is masked until the cache expired,
        letting a guarded call run stale code in exactly the window the guard must
        cover. Guarded tools are rare (procedure_store only), so re-reading the
        tiny update_history row each not-yet-stale call is negligible.

        Fail-open: any uncertainty (no db, unknown spawn identity, empty history,
        parse/db error) returns False — the guard fires only on POSITIVE
        confirmation of staleness, never on a failed check.
        """
        if self._is_stale_latched:
            return True
        if self._db is None:
            return False
        from genesis.observability import mcp_spawn_identity as si

        sc, sa = si.spawn_commit(), si.spawn_at()
        if not sc or not sa:
            return False
        try:
            from genesis.db.crud.update_history import last_successful_update

            row = await last_successful_update(self._db)
            if row is not None:
                completed_at, new_commit = row
                if is_stale(sc, sa, completed_at, new_commit):
                    self._is_stale_latched = True
                    return True
        except Exception:
            logger.debug(
                "MCP staleness check failed; treating as not stale", exc_info=True
            )
        return False
