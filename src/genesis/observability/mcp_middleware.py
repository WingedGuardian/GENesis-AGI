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

from genesis.observability.provider_activity import ProviderActivityTracker

logger = logging.getLogger(__name__)


class InstrumentationMiddleware(Middleware):
    """Records per-tool invocation metrics to ProviderActivityTracker.

    Provider names use the format "mcp.{server_name}.{tool_name}" to keep
    them distinct from LLM provider entries (e.g., "llm.gemini_flash").

    All tracking is fire-and-forget — tracker errors never propagate to
    the MCP tool handler.

    Per-call transaction boundary (WS-15 follow-up): when constructed with the
    server's long-lived ``db`` connection, each tool call ends with a
    commit-on-success / rollback-on-error. This releases the read snapshot that
    a read-only tool would otherwise leave open in deferred-isolation mode —
    which pins the SQLite WAL checkpoint and makes later writes fail
    "database is locked". Writes already commit at the CRUD layer before the
    tool returns, so the commit here only closes a *trailing* read txn (it can
    never discard a tool's own write); rollback-on-error discards a partial.
    DB errors in this boundary never propagate to the tool handler.
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

    async def on_call_tool(self, context, call_next):
        """Wrap tool invocations with timing, error tracking, and a per-call
        commit/rollback boundary (see class docstring)."""
        tool_name = context.message.name
        provider = f"mcp.{self._server}.{tool_name}"
        t0 = time.monotonic()
        success = True
        try:
            result = await call_next(context)
            return result
        except Exception:
            success = False
            raise
        finally:
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
