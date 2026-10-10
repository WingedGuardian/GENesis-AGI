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
    INTERACTIVE = "interactive"


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
# Reviewed, closed ordinary interactive scope. New tool names require admission;
# availability never substitutes for the tool's existing authorization gates.
HEALTH_INTERACTIVE_TOOLS = frozenset({
    "bench_status", "board_item", "board_promote",
    "board_status", "bootstrap_manifest", "browser_clear_domain",
    "browser_click", "browser_collaborate", "browser_fill",
    "browser_navigate", "browser_press_key", "browser_run_js",
    "browser_screenshot", "browser_sessions", "browser_snapshot",
    "browser_upload", "build_lane_status", "calibration_status",
    "campaign_create", "campaign_list", "campaign_pause",
    "campaign_resume", "campaign_status", "campaign_update",
    "codebase_navigate", "cognitive_modification_rollback", "cognitive_modification_status",
    "contributor_issue_propose", "db_schema", "deliberate",
    "direct_session_list", "direct_session_run", "direct_session_status",
    "ego_calibration_status", "ego_decision", "ego_directive",
    "ego_focus_reset", "ego_goal_create", "ego_goal_list",
    "ego_goal_progress", "ego_goal_update", "evo_run",
    "experiment_run", "experiment_status", "follow_up_create",
    "follow_up_list", "follow_up_update", "health_status",
    "immunity_status", "inbox_digest", "infrastructure_profile",
    "intake_complete", "j9_eval_status", "job_health",
    "loop_closure_status", "module_call", "module_list",
    "open_question_block", "open_question_list", "open_question_raise",
    "open_question_resolve", "provider_activity", "reflex_signal_resolve",
    "reflex_status", "session_address", "session_charter",
    "session_charter_update", "session_config", "session_ledger_add",
    "session_ledger_update", "settings_get", "settings_list",
    "settings_update", "skill_replay_run", "subsystem_heartbeats",
    "task_detail", "task_list", "task_submit",
    "update_history_recent", "web_agent", "web_fetch",
    "web_search", "zero_drop_ack", "zero_drop_status",
})
MEMORY_INTERACTIVE_TOOLS = frozenset({
    "bookmark_shelve", "bookmark_unshelve", "conversation_history",
    "document_index", "document_query", "entity_adjudication_apply",
    "entity_adjudication_approve", "entity_adjudication_list", "entity_adjudication_reject",
    "knowledge_ingest", "knowledge_ingest_batch", "knowledge_ingest_source",
    "knowledge_recall", "knowledge_status", "locate",
    "memory_core_facts", "memory_expand", "memory_extract",
    "memory_proactive", "memory_recall", "memory_stats",
    "memory_store", "memory_supersede", "memory_synthesize",
    "observation_query", "observation_resolve", "observation_write",
    "procedure_recall", "procedure_store", "reference_delete",
    "reference_export", "reference_lookup", "reference_store",
    "resume_review",
})

# The launcher removes these identities. Startup and lazy routing must not
# restore them from secrets.env or change the launcher's selected runtime root.
EXTERNAL_SECRET_BLOCKED_KEYS = frozenset({
    "GENESIS_CC_SESSION", "GENESIS_SESSION_ID", "GENESIS_SESSION_ORIGIN",
    "GENESIS_SESSION_SUPERVISED", "GENESIS_SLOT", "GENESIS_TRACE_ID",
    "GENESIS_PARENT_SPAN_ID", "CLAUDE_CODE_SESSION_ID", "GENESIS_REPO_ROOT",
})

_PROFILE_TOOLS = {
    ExternalProfile.EXTERNAL: {"health": HEALTH_FLOOR_TOOLS, "memory": MEMORY_FLOOR_TOOLS},
    # Owner-approved capability staging: later reviewed writer slices widen only
    # this explicit role; the ordinary external profile retains its current floor.
    ExternalProfile.VALIDATOR: {"health": HEALTH_FLOOR_TOOLS, "memory": MEMORY_FLOOR_TOOLS},
    ExternalProfile.INTERACTIVE: {"health": HEALTH_INTERACTIVE_TOOLS, "memory": MEMORY_INTERACTIVE_TOOLS},
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
