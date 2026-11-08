"""Log every MCP tool call a labelled client makes to ``client_tool_calls``.

Installed on all four Genesis MCP servers by
:func:`genesis.observability.mcp_middleware.install_instrumentation`. On each
call it reads the caller through :func:`genesis.caller_context.caller_context`;
a call with no caller profile (every Claude Code session) logs nothing. Every
labelled call gets one row: owner-mode Codex (``codex``) today, and the
shared-service client profiles (``external``, ``validator``, ``interactive``) if
that mode is ever switched on. A row holds client, server,
tool, the time it started, how it ended, and the id of what it wrote for the few
writer tools whose result is known to carry one. Arguments are never stored.

``outcome`` says how the call ended, not whether the tool succeeded: many tools
report failure inside a normal return (``{"error": ...}``, ``"Refused: ..."``),
and those read ``returned``.

The write runs after the call on its own short-lived connection, so it never
joins the tool's transaction, and it is shielded from cancellation: a cancelled
call is exactly the ambiguous case an audit log exists for. A failed or skipped
write is a warning and never changes the tool's result.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

import anyio
from fastmcp.server.middleware import Middleware

logger = logging.getLogger(__name__)

# Writer tools whose result is known to carry the id of the row they wrote, and
# the key it is under. A ``-> str`` tool's return is wrapped by fastmcp as
# ``{"result": ...}``. Every other tool records no reference: guessing from the
# shape of arbitrary results stores tokens, card numbers and dates.
_REF_KEYS = {
    ("memory", "memory_store"): "result",
    ("memory", "reference_store"): "result",
    ("memory", "observation_write"): "result",
    ("health", "follow_up_create"): "id",
    ("health", "ego_decision"): "decision_id",
}
# The id formats those tools mint: a UUID, or 32 or 16 lowercase hex characters.
_ID_SHAPE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}|[0-9a-f]{16}"
)


# One second of lock wait for an audit row; see the comment in ``_log``.
_LOG_BUSY_TIMEOUT_MS = 1000
# Tool names are client-supplied (a call to an unknown name still raises and is
# logged). Registered Genesis tool names are under 40 characters; anything over
# this is stored as a marker naming its length, never cut to fit.
_TOOL_NAME_MAX = 128


def _tool_name(tool: str) -> str:
    if len(tool) <= _TOOL_NAME_MAX:
        return tool
    return f"<tool name omitted: {len(tool)} chars>"


def result_ref(server: str, tool: str, result: Any) -> str | None:
    """The id an allowlisted writer tool returned, or None."""
    key = _REF_KEYS.get((server, tool))
    structured = getattr(result, "structured_content", None)
    if key is None or not isinstance(structured, dict):
        return None
    value = structured.get(key)
    if isinstance(value, str) and _ID_SHAPE.fullmatch(value):
        return value
    return None


class ClientCallLogMiddleware(Middleware):
    """Append one ``client_tool_calls`` row per call from a labelled client."""

    def __init__(self, server_name: str) -> None:
        self._server = server_name
        self._warned_missing = False
        self._warned_failure = False

    async def on_call_tool(self, context, call_next):
        from genesis.caller_context import caller_context

        client = caller_context().profile
        if not client:
            return await call_next(context)
        started = datetime.now(UTC).isoformat()
        outcome = "raised"
        result = None
        try:
            result = await call_next(context)
            outcome = "returned"
            return result
        except (asyncio.CancelledError, anyio.get_cancelled_exc_class()):
            outcome = "cancelled"
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await self._log(client, context.message.name, started, outcome, result)

    async def _log(
        self,
        client: str,
        tool: str,
        started: str,
        outcome: str,
        result: Any,
    ) -> None:
        try:
            from genesis.db.connection import get_raw_db
            from genesis.db.crud import client_tool_calls
            from genesis.env import genesis_db_path

            path = genesis_db_path()
            if not path.exists():
                # A server running without a database must not have one created
                # here as a side effect of logging.
                self._warn_missing(client, tool, "no database file")
                return
            async with get_raw_db(path) as conn:
                # This write sits on the call's response path and cannot be
                # cancelled, so it must not inherit the connection's long busy
                # timeout: a contended database costs one missing row, never a
                # call stalled for seconds.
                await conn.execute(f"PRAGMA busy_timeout = {_LOG_BUSY_TIMEOUT_MS}")
                written = await client_tool_calls.record(
                    conn,
                    client=client,
                    server=self._server,
                    tool=_tool_name(tool),
                    called_at=started,
                    outcome=outcome,
                    result_ref=result_ref(self._server, tool, result)
                    if outcome == "returned"
                    else None,
                )
            if not written:
                self._warn_missing(client, tool, "table not migrated yet")
        except Exception:
            # Full traceback once; after that a persistent failure (a
            # quarantined database, a locked file) logs one line per call.
            logger.warning(
                "client call log: could not record %s.%s for %s",
                self._server,
                _tool_name(tool),
                client,
                exc_info=not self._warned_failure,
            )
            self._warned_failure = True

    def _warn_missing(self, client: str, tool: str, why: str) -> None:
        if not self._warned_missing:
            self._warned_missing = True
            logger.warning(
                "client call log: %s; calls by %s on %s are not being recorded (first: %s)",
                why,
                client,
                self._server,
                _tool_name(tool),
            )
