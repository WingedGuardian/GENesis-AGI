"""Session config builder — per-type CC session configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.cc.types import CCModel, EffortLevel

if TYPE_CHECKING:
    pass

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

# MCP server profiles — which servers each session type needs.
# Module-level constant (immutable intent), consistent with _READONLY_DISALLOWED.
_MCP_PROFILES: dict[str, list[str]] = {
    "reflection": ["genesis-health", "genesis-memory"],
    "sentinel": ["genesis-health", "genesis-memory", "genesis-outreach"],
    "interop": ["genesis-health", "genesis-memory"],
}


class SessionConfigBuilder:
    """Builds CC session configurations per type."""

    def build_reflection_config(self, depth: str = "deep") -> dict:
        """Config for reflection sessions: read-only tools, high effort."""
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
            "disallowed_tools": _READONLY_DISALLOWED,
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
        """Load SOUL.md identity content."""
        from pathlib import Path

        soul_path = Path(__file__).resolve().parent.parent / "identity" / "SOUL.md"
        if soul_path.exists():
            return soul_path.read_text(encoding="utf-8")
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

        # Generate from template.
        try:
            genesis_root = str(config_dir.parent)
            template_text = template_path.read_text(encoding="utf-8")
            resolved = template_text.replace("{{GENESIS_ROOT}}", genesis_root)
            full_config = json.loads(resolved)

            filtered = {
                "mcpServers": {
                    k: v
                    for k, v in full_config.get("mcpServers", {}).items()
                    if k in servers
                }
            }

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
