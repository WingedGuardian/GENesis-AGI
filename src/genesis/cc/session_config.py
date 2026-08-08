"""Session config builder — per-type CC session configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.cc.types import CCModel, EffortLevel

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Tools to block in read-only sessions (reflection, surplus).
# Uses a blacklist so MCP tools (genesis-health, genesis-memory, etc.) and
# future CC built-in tools are available without explicit listing.
# Aligns with CLAUDE.md "don't handicap autonomous sessions" principle.
_READONLY_DISALLOWED = [
    "Write",
    "Edit",
    "Bash",
    "NotebookEdit",
]

# NOTE: Destructive git operations (force push, hard reset, clean) are guarded
# by PreToolUse hooks in .claude/settings.json, NOT by disallowed_tools.
# disallowed_tools matches tool NAMES (e.g. "Bash"), not command substrings.
# Hooks fire for ALL sessions including claude -p, so protection is global.

# ── Reflection tool lockdown ──────────────────────────────────────────────
# Autonomous reflection sessions (DEEP/STRATEGIC) are READ-ONLY: they read
# freely to investigate, and their ONLY write is an observation (the
# observation_write tool for interaction_theme observations, plus the structured
# `observations` output field that the reflection output router parses and writes
# server-side). Every OTHER registered MCP tool is DENIED — the denylist is
# DERIVED as (live registry − read-allowlist − observation_write), so a write
# tool a future PR adds to either server is auto-denied here without a code
# change. This gives a denylist the safety of an allowlist, which matters because
# `--allowedTools` is NOT a strict allowlist under `--dangerously-skip-permissions`
# (verified empirically 2026-08-07 via the init-event tool list: --allowedTools
# left Bash available; only --disallowedTools removes a tool). The read-allowlist
# below is the maintained artifact; tests/test_cc/test_reflection_tool_scope.py
# enforces it stays complete against the live registry.
_REFLECTION_READ_MCP: frozenset[str] = frozenset({
    # genesis-health — status / list / get / query (no mutation)
    "bench_status", "bootstrap_manifest", "build_lane_status", "calibration_status",
    "campaign_list", "campaign_status", "codebase_navigate", "cognitive_modification_status",
    "db_schema", "direct_session_list", "direct_session_status", "ego_calibration_status",
    "ego_goal_list", "experiment_status", "health_alerts", "health_errors", "health_status",
    "immunity_status", "inbox_digest", "infrastructure_profile", "j9_eval_status", "job_health",
    "loop_closure_status", "module_list", "provider_activity", "reflex_status",
    "session_charter", "settings_get", "settings_list",
    "subsystem_heartbeats", "task_detail", "task_list", "update_history_recent",
    "follow_up_list", "web_fetch", "web_search",
    # genesis-memory — recall / query / lookup (no mutation)
    "conversation_history", "document_query", "knowledge_recall", "knowledge_status",
    "locate", "memory_expand", "memory_proactive", "memory_recall",
    "memory_stats", "observation_query", "procedure_recall", "reference_lookup",
    "reference_export",
})
# The single sanctioned reflection write. The rest of reflection's observations
# flow through the parsed `observations` output field (written server-side), not a tool.
_REFLECTION_WRITE_MCP: frozenset[str] = frozenset({"observation_write"})

# The MCP servers a reflection session loads (mirrors _MCP_PROFILES["reflection"]).
_REFLECTION_MCP_SERVERS: tuple[tuple[str, str], ...] = (
    ("genesis-health", "genesis.mcp.health"),
    ("genesis-memory", "genesis.mcp.memory"),
)

# Built-in (non-MCP) write/action tools reflection must NOT have. Read built-ins
# (Read/Grep/Glob/WebFetch/WebSearch/ToolSearch/CronList/Task{Get,List,Output}/
# ListMcpResourcesTool/ReadMcpResource*Tool) are left available. CC internals
# aren't introspectable, so this is an explicit list; the scope test re-checks it.
# Task/Workflow/Skill are denied because they SPAWN — a subagent would escape the
# lockdown.
_REFLECTION_DENY_BUILTINS: tuple[str, ...] = (
    "Bash", "Write", "Edit", "NotebookEdit",
    "Task", "Workflow", "Skill",
    "SendMessage", "ReportFindings",
    "CronCreate", "CronDelete", "ScheduleWakeup", "Monitor",
    "TaskCreate", "TaskUpdate", "TaskStop",
    "RemoteTrigger", "PushNotification", "DesignSync",
    "EnterWorktree", "ExitWorktree",
)


def _registered_mcp_tool_names(modpath: str) -> list[str]:
    """Tool names registered on a FastMCP server module's ``mcp`` object.

    Late import (avoids a circular import at module load); tool registration
    happens at import time via the ``@mcp.tool()`` decorators, and importing the
    module does NOT start the server (that is ``mcp.run()``).
    """
    mod = __import__(modpath, fromlist=["mcp"])
    server = mod.mcp
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if isinstance(tools, dict):
        return list(tools.keys())
    raise RuntimeError(f"cannot enumerate MCP tools for {modpath!r}")


def render_mcp_servers(
    template_path: Path, genesis_root: str, servers: set[str] | None = None,
) -> dict:
    """Render ``config/mcp.json.template`` into an MCP-config dict.

    Pure function: no cache, no file writes — the single owner of the
    ``{{GENESIS_ROOT}}`` substitution contract. ``build_mcp_config`` layers
    its mtime cache on top; the eval bench calls this directly to inject
    per-run ``env`` blocks (a shared ``.generated/`` cache is wrong for
    per-run configs). ``servers=None`` keeps every server; otherwise filter
    to the named subset.
    """
    import json

    template_text = template_path.read_text(encoding="utf-8")
    resolved = template_text.replace("{{GENESIS_ROOT}}", genesis_root)
    full_config = json.loads(resolved)
    all_servers = full_config.get("mcpServers", {})
    if servers is not None:
        all_servers = {k: v for k, v in all_servers.items() if k in servers}
    return {"mcpServers": all_servers}


# MCP server profiles — which servers each session type needs.
# Module-level constant (immutable intent), consistent with _READONLY_DISALLOWED.
_MCP_PROFILES: dict[str, list[str]] = {
    "reflection": ["genesis-health", "genesis-memory"],
    # research bg sessions: reflection servers + genesis-recon (the discovery
    # engine — GitHub/model-intel/skill scanning). Full read+write recon; the
    # research disallow list already omits _NO_RECON_WRITES.
    "research": ["genesis-health", "genesis-memory", "genesis-recon"],
    "user_reflection": ["genesis-memory"],  # User ego: memory only, no health tools
    "sentinel": ["genesis-health", "genesis-memory", "genesis-outreach"],
    "campaign": ["genesis-health", "genesis-memory", "genesis-outreach"],
    "community-responder": ["genesis-health", "genesis-outreach", "discord-bot"],
    "interop": ["genesis-health", "genesis-memory"],
    "mail": ["genesis-outreach"],  # Perimeter: outreach only, no memory/health
}


class SessionConfigBuilder:
    """Builds CC session configurations per type."""

    def build_reflection_disallowed(self) -> list[str]:
        """Denylist that makes a reflection session read-only + observation-writing.

        MCP tools: DERIVED as (live registry − read-allowlist − observation_write),
        so a future write tool on either reflection server is auto-denied. Built-ins:
        the explicit write/action set (_REFLECTION_DENY_BUILTINS). If registry
        enumeration fails, fall back to denying both MCP servers wholesale
        (``mcp__<server>__*``) — fail-closed: observations still flow through the
        parsed ``observations`` output field, and no write tool can leak.
        """
        disallowed: list[str] = list(_REFLECTION_DENY_BUILTINS)
        try:
            for server, modpath in _REFLECTION_MCP_SERVERS:
                for name in _registered_mcp_tool_names(modpath):
                    if name in _REFLECTION_READ_MCP or name in _REFLECTION_WRITE_MCP:
                        continue
                    disallowed.append(f"mcp__{server}__{name}")
        except Exception:
            logger.error(
                "Reflection MCP tool enumeration failed — denying both MCP servers "
                "wholesale (fail-closed). DEEP investigation tools are lost until "
                "this is fixed, but no write tool can leak.",
                exc_info=True,
            )
            disallowed = list(_REFLECTION_DENY_BUILTINS) + [
                f"mcp__{server}__*" for server, _ in _REFLECTION_MCP_SERVERS
            ]
        return disallowed

    def build_reflection_config(self, depth: str = "deep") -> dict:
        """Config for reflection sessions: read-only tools + observation_write."""
        if depth == "strategic":
            model = CCModel.OPUS
            effort = EffortLevel.MAX
        else:
            model = CCModel.OPUS
            effort = EffortLevel.HIGH
        system_prompt = self._load_identity_block()

        return {
            "model": str(model),
            "effort": str(effort),
            "system_prompt": system_prompt,
            "disallowed_tools": self.build_reflection_disallowed(),
            "skip_permissions": True,
        }

    def build_task_config(
        self,
        task_description: str,
        skill_names: list[str] | None = None,
    ) -> dict:
        """Config for task sessions: identity + skills, full tool access.

        Destructive git ops are guarded by PreToolUse hooks, not disallowed_tools.
        """
        system_prompt = self._load_identity_block()

        # Load skill content
        if skill_names:
            from genesis.learning.skills.wiring import load_skill

            for name in skill_names:
                content = load_skill(name)
                if content:
                    system_prompt += f"\n\n## Skill: {name}\n{content}"

        return {
            "model": str(CCModel.SONNET),
            "effort": str(EffortLevel.MEDIUM),
            "system_prompt": system_prompt,
            "skip_permissions": True,
        }

    def build_surplus_config(self) -> dict:
        """Config for surplus/brainstorm sessions: read + search only."""
        return {
            "model": str(CCModel.SONNET),
            "effort": str(EffortLevel.MEDIUM),
            "system_prompt": self._load_identity_block(),
            "disallowed_tools": _READONLY_DISALLOWED,
            "skip_permissions": True,
        }

    def _load_identity_block(self) -> str:
        """Load SOUL.md + VOICE.md identity content."""
        from pathlib import Path

        identity_dir = Path(__file__).resolve().parent.parent / "identity"
        soul_path = identity_dir / "SOUL.md"
        voice_path = identity_dir / "VOICE.md"
        parts: list[str] = []
        if soul_path.exists():
            parts.append(soul_path.read_text(encoding="utf-8"))
        if voice_path.exists():
            parts.append(voice_path.read_text(encoding="utf-8"))
        if parts:
            return "\n\n---\n\n".join(parts)
        logger.warning("SOUL.md not found, using minimal identity")
        return "You are Genesis, an autonomous AI agent."

    def build_mcp_config(self, profile: str = "full") -> str | None:
        """Generate MCP config file path for a session profile.

        Profiles:
          - ``"none"``: no MCP servers (LIGHT reflection).
          - ``"reflection"``: health + memory only (DEEP/STRATEGIC).
          - ``"full"``: all servers — returns *None* so CC uses its default config.

        Returns a file path string or *None*.
        """
        import json
        import os
        from pathlib import Path

        config_dir = Path(__file__).resolve().parent.parent.parent.parent / "config"

        if profile == "full":
            return None

        if profile == "none":
            return str(config_dir / "no_mcp.json")

        servers = _MCP_PROFILES.get(profile)
        if not servers:
            logger.warning("Unknown MCP profile %r, using full", profile)
            return None

        generated_dir = config_dir / ".generated"
        generated_path = generated_dir / f"{profile}_mcp.json"
        template_path = config_dir / "mcp.json.template"

        # Cache: skip regeneration if generated file is fresh.
        if (
            generated_path.exists()
            and template_path.exists()
            and generated_path.stat().st_mtime >= template_path.stat().st_mtime
        ):
            return str(generated_path)

        # Generate from template (shared substitution contract lives in
        # render_mcp_servers; this method only adds the mtime cache).
        try:
            filtered = render_mcp_servers(
                template_path, str(config_dir.parent), set(servers),
            )

            os.makedirs(generated_dir, exist_ok=True)
            generated_path.write_text(
                json.dumps(filtered, indent=2) + "\n", encoding="utf-8",
            )
            return str(generated_path)
        except Exception:
            logger.warning(
                "MCP config generation failed for profile %r, using full",
                profile,
                exc_info=True,
            )
            return None

    # GROUNDWORK(hook-inheritance): Hook inheritance for CC sessions
    def build_hook_config(self) -> dict | None:
        """Placeholder for hook config inheritance. Needs CC features."""
        return None
