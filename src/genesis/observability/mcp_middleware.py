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

from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as ToolValidationError
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError as PydanticValidationError

from genesis.observability.commit_identity import is_stale
from genesis.observability.commit_identity import (
    same_commit as _same_commit,  # noqa: F401 — re-exported for Part A's guard test
)
from genesis.observability.mcp_arg_diagnostics import absorbed_parameter_hint
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
    clean return and rolls back otherwise. What that boundary can and cannot end
    (measured on a scratch database, Python 3.12 / SQLite 3.45 / aiosqlite 0.22,
    legacy implicit-BEGIN isolation, WAL):

    - commit/rollback end an open WRITE transaction, i.e. one a DML statement
      began and nothing has committed yet;
    - a read-only call opens no transaction (a SELECT does not BEGIN in this
      mode), so for it the boundary is a no-op;
    - a partially read multi-row SELECT pins the WAL checkpoint until its cursor
      is closed or read to the end, and commit/rollback do NOT release that pin
      (a ``LIMIT 1`` read with ``fetchone()`` steps to the end and pins nothing).

    Most writes already commit at the CRUD layer before the tool returns, so the
    commit here only flushes a write the tool left uncommitted (it can never
    discard a tool's own write); any non-clean exit — a raised ``Exception``,
    or a ``BaseException`` no clause catches (``asyncio.CancelledError``, ``SystemExit``, …) — rolls back the
    possibly-partial write instead of committing it, but only when no other call
    is in flight on the shared connection (a rollback then would discard a
    concurrent call's uncommitted write; see ``on_call_tool``). The boundary swallows
    nothing: a ``BaseException`` is not caught at all, and the one ``except
    Exception`` clause always re-raises — sometimes as a ``ToolError`` chained to
    the original, when the argument diagnosis below can name a better cause. DB
    errors in this boundary never propagate to the tool handler.
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
        # Tool calls in flight on this server's shared connection. The MCP SDK
        # runs each request in its own task, so calls overlap; a failure
        # rollback is only safe when no other call can have an uncommitted
        # statement on the same connection (see on_call_tool's finally).
        self._calls_in_flight: int = 0
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
        # and commit a partial). Nothing is swallowed: the ``except Exception``
        # below always re-raises, and a cancellation is not caught at all.
        success = False
        self._calls_in_flight += 1
        try:
            if tool_name in GUARDED_MCP_TOOLS:
                # Raises ToolError (block mode) if this subprocess is running
                # stale code. Inside the try so a block is recorded as a failed
                # call. Its update_history read is LIMIT 1 + fetchone(), which
                # opens no transaction and leaves no snapshot pinned.
                await self._enforce_freshness(tool_name)
            result = await call_next(context)
            success = True
            return result
        except Exception as exc:
            # success stays False — it is only set True after a clean return, so
            # there is nothing to reset here.
            # A "missing argument" error is sometimes the WRONG explanation: if a
            # long free-text argument was emitted with a mismatched closing tag,
            # it swallowed every parameter after it, and those are then reported
            # missing though they were sent. Replace the message with the real
            # cause when the evidence is in the arguments themselves.
            #
            # Only ever runs on a call that is ALREADY failing, never alters
            # arguments, and returns None on every uncertain path — so it cannot
            # make a well-formed call fail, and a false positive costs only a
            # slightly wrong explanation on a call that was refused regardless.
            hint = None
            try:
                # v4 wraps binding errors in its own ValidationError. Keep the
                # structured Pydantic error for the diagnosis; never unwrap a
                # body exception or infer missing names from rendered prose.
                diagnosis_error = exc
                if isinstance(exc, ToolValidationError) and isinstance(exc.__cause__, PydanticValidationError):
                    diagnosis_error = exc.__cause__
                hint = absorbed_parameter_hint(
                    diagnosis_error,
                    getattr(context.message, "arguments", None) or {},
                    tool_name,
                )
            except Exception:  # diagnosis must never replace the real error
                # WARNING, not debug: this only fires when the diagnosis ITSELF
                # is broken, on a path that is already an error, so it cannot
                # spam — and at debug a persistent bug here would stay invisible
                # across all four servers.
                logger.warning(
                    "argument diagnosis failed for %s", provider, exc_info=True
                )
            if hint:
                raise ToolError(hint) from exc
            raise
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
            #
            # The failure rollback runs only when this is the ONLY call in
            # flight. The connection is shared and SerializedConnection lets
            # two calls' statements interleave, so a rollback while another
            # call sits between its execute and its own commit discards THAT
            # call's write while it goes on to report success (reproduced by
            # tests/test_observability/test_mcp_middleware_concurrency.py).
            # Skipping it under concurrency leaves this call's partial
            # statement, if any, for the next commit to flush: the same
            # outcome a peer's commit already produced before this guard. It
            # leaves no read snapshot behind: a read-only call opens no
            # transaction, and a cursor-pinned snapshot is released only by
            # closing the cursor, never by commit or rollback (class docstring).
            # Per-call atomicity on one shared connection needs per-request
            # connections, which this boundary cannot provide; that residual is
            # tracked as P2b in the P4 shared-service design. Only calls
            # through this middleware are counted.
            try:
                if self._db is not None:
                    try:
                        if success:
                            await self._db.commit()
                        elif self._calls_in_flight == 1:
                            await self._db.rollback()
                        else:
                            logger.warning(
                                "Skipped rollback after failed %s: %d other "
                                "call(s) in flight on the shared connection",
                                provider, self._calls_in_flight - 1,
                            )
                    except Exception:
                        logger.warning(
                            "DB %s after %s failed",
                            "commit" if success else "rollback",
                            provider, exc_info=True,
                        )
            finally:
                # Decrement first and unconditionally: the record below can
                # raise BaseException too, and a leaked count would disable the
                # failure rollback for the life of the server process.
                self._calls_in_flight -= 1
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
        # ToolError is imported at module level now (the absorbed-parameter
        # diagnosis needs it too), so the local re-import here would shadow it
        # with the identical symbol.
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
