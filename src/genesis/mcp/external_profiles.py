"""Server-side tool scope for external clients, independent of client filtering.

Profiles constrain MCP requests in these standalone processes. They are not
authentication or isolation from an operator with full host access.

FastMCP task-augmented calls bypass ordinary tool middleware. Genesis tools
currently forbid that mode; enabling tasks requires a separate admission
boundary before this profile can cover them.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware


class ExternalProfile(StrEnum):
    EXTERNAL = "external"
    VALIDATOR = "validator"


logger = logging.getLogger(__name__)

# Recall can update retrieval metadata and invoke corrective retrieval. These
# sets describe admitted tool capabilities, not side-effect-free operations.
HEALTH_FLOOR_TOOLS = frozenset({
    "health_status", "bootstrap_manifest", "subsystem_heartbeats", "job_health", "provider_activity",
})
MEMORY_FLOOR_TOOLS = frozenset({
    "memory_recall", "memory_expand", "memory_core_facts", "memory_stats",
    "knowledge_recall", "knowledge_status", "procedure_recall", "observation_query",
})
_PROFILE_TOOLS = {
    ExternalProfile.EXTERNAL: {"health": HEALTH_FLOOR_TOOLS, "memory": MEMORY_FLOOR_TOOLS},
    # Owner-approved capability staging: later reviewed writer slices widen only
    # this explicit role; the ordinary external profile retains its current floor.
    ExternalProfile.VALIDATOR: {"health": HEALTH_FLOOR_TOOLS, "memory": MEMORY_FLOOR_TOOLS},
}


def profile_tools(server: str, profile: ExternalProfile | str) -> frozenset[str]:
    """Closed roles and servers; validator writers arrive with their own reviews."""
    tools = _PROFILE_TOOLS[ExternalProfile(profile)]
    try:
        return tools[server]
    except KeyError as exc:
        raise ValueError(f"External client profiles do not support server {server!r}") from exc


class ExternalProfileMiddleware(Middleware):
    """Filter discovery AND refuse direct calls before entering a tool body."""

    def __init__(self, server: str, profile: ExternalProfile | str):
        self.profile = ExternalProfile(profile)
        self.allowed = profile_tools(server, self.profile)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool for tool in tools if tool.key in self.allowed]

    async def on_call_tool(self, context, call_next):
        if context.message.name not in self.allowed:
            logger.warning("External profile %s refused tool %r", self.profile, context.message.name)
            raise ToolError("Tool is unavailable to this external client profile")
        return await call_next(context)
