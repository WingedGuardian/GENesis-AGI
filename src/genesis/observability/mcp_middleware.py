"""FastMCP instrumentation middleware — tracks per-tool call metrics.

Automatically records call counts, latency, and error rates for every MCP
tool invocation.  Uses the "mcp.{server}.{tool}" namespace to avoid
collisions with provider names in the shared ProviderActivityTracker.

Requires FastMCP >= 2.9 (middleware support).
"""

from __future__ import annotations

import logging
import time

from fastmcp.server.middleware import Middleware

from genesis.observability.provider_activity import ProviderActivityTracker

logger = logging.getLogger(__name__)


class InstrumentationMiddleware(Middleware):
    """Records per-tool invocation metrics to ProviderActivityTracker.

    Provider names use the format "mcp.{server_name}.{tool_name}" to keep
    them distinct from LLM provider entries (e.g., "llm.gemini_flash").

    All tracking is fire-and-forget — tracker errors never propagate to
    the MCP tool handler.
    """

    def __init__(self, tracker: ProviderActivityTracker, server_name: str) -> None:
        self._tracker = tracker
        self._server = server_name

    async def on_call_tool(self, context, call_next):
        """Wrap tool invocations with timing and error tracking."""
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
