"""ProtectedPathRegistry — classifies file paths by protection level.

Enforcement is defense-in-depth (3 layers):
  Layer 1: CC PreToolUse hooks in .claude/settings.json (architectural)
  Layer 2: System prompt injection for relay-dispatched sessions (LLM)
  Layer 3: Post-session git diff audit (detective)

This module provides the classification API consumed by all layers.
It has no knowledge of its consumers (CCInvoker, hooks, prompt assembler).
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path, PurePosixPath

import yaml

from genesis.autonomy.types import ProtectedPathRule, ProtectionLevel

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "protected_paths.yaml"


class ProtectedPathRegistry:
    """Classifies file paths into protection levels based on YAML config.

    Thread-safe: rules are loaded once at init and are immutable.
    """

    def __init__(self, rules: list[ProtectedPathRule] | None = None) -> None:
        self._rules: tuple[ProtectedPathRule, ...] = tuple(rules or [])

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> ProtectedPathRegistry:
        """Load protection rules from YAML config file."""
        config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
        if not config_path.exists():
            logger.warning(
                "Protected paths config not found at %s — all paths treated as NORMAL",
                config_path,
            )
            return cls(rules=[])

        try:
            data = yaml.safe_load(config_path.read_text())
        except (yaml.YAMLError, OSError):
            logger.error(
                "Failed to parse protected paths config at %s — all paths treated as NORMAL",
                config_path,
                exc_info=True,
            )
            return cls(rules=[])

        rules: list[ProtectedPathRule] = []
        if not isinstance(data, dict):
            logger.error("Protected paths config is not a dict — all paths NORMAL")
            return cls(rules=[])

        for level_str in ("critical", "sensitive"):
            level = ProtectionLevel(level_str)
            entries = data.get(level_str, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and "pattern" in entry:
                    rules.append(ProtectedPathRule(
                        pattern=entry["pattern"],
                        level=level,
                        reason=entry.get("reason", ""),
                    ))
                elif isinstance(entry, str):
                    rules.append(ProtectedPathRule(pattern=entry, level=level))

        logger.info(
            "Loaded %d protected path rules (%d critical, %d sensitive)",
            len(rules),
            sum(1 for r in rules if r.level is ProtectionLevel.CRITICAL),
            sum(1 for r in rules if r.level is ProtectionLevel.SENSITIVE),
        )
        return cls(rules=rules)

    def classify(self, path: str) -> ProtectionLevel:
        """Classify a file path. Returns the highest protection level matched.

        Paths are normalized before matching: resolved relative to repo root,
        forward slashes, no leading './'.
        """
        normalized = self._normalize(path)

        if normalized.startswith("..") or normalized.startswith("/"):
            return ProtectionLevel.CRITICAL

        # Check most restrictive first — return on first CRITICAL match
        for rule in self._rules:
            if rule.level is ProtectionLevel.CRITICAL and self._matches(normalized, rule.pattern):
                return ProtectionLevel.CRITICAL

        for rule in self._rules:
            if rule.level is ProtectionLevel.SENSITIVE and self._matches(normalized, rule.pattern):
                return ProtectionLevel.SENSITIVE

        return ProtectionLevel.NORMAL

    def classify_with_reason(self, path: str) -> tuple[ProtectionLevel, str]:
        """Classify and return the matching rule's reason."""
        normalized = self._normalize(path)

        if normalized.startswith("..") or normalized.startswith("/"):
            return ProtectionLevel.CRITICAL, "Path traversal outside project root"

        for rule in self._rules:
            if rule.level is ProtectionLevel.CRITICAL and self._matches(normalized, rule.pattern):
                return ProtectionLevel.CRITICAL, rule.reason

        for rule in self._rules:
            if rule.level is ProtectionLevel.SENSITIVE and self._matches(normalized, rule.pattern):
                return ProtectionLevel.SENSITIVE, rule.reason

        return ProtectionLevel.NORMAL, ""

    def get_rules(self, level: ProtectionLevel | None = None) -> list[ProtectedPathRule]:
        """Return all rules, optionally filtered by level."""
        if level is None:
            return list(self._rules)
        return [r for r in self._rules if r.level is level]

    def format_for_prompt(self) -> str:
        """Format protection rules as text for system prompt injection (Layer 2)."""
        lines = ["## Protected Paths", ""]
        lines.append("You are operating via relay. The following paths CANNOT be modified "
                      "from this channel. If you need to modify them, explain why and tell "
                      "the user to use a CC CLI session instead.")
        lines.append("")

        critical = self.get_rules(ProtectionLevel.CRITICAL)
        if critical:
            lines.append("### CRITICAL (never modify via relay)")
            for rule in critical:
                lines.append(f"- `{rule.pattern}` — {rule.reason}")
            lines.append("")

        sensitive = self.get_rules(ProtectionLevel.SENSITIVE)
        if sensitive:
            lines.append("### SENSITIVE (requires explicit user approval)")
            for rule in sensitive:
                lines.append(f"- `{rule.pattern}` — {rule.reason}")
            lines.append("")

        return "\n".join(lines)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    @staticmethod
    def _normalize(path: str) -> str:
        """Normalize path for matching: resolve .., strip leading ./, forward slashes."""
        import posixpath

        # Resolve .. segments (pure string op, no filesystem access)
        normalized = posixpath.normpath(path)
        p = PurePosixPath(normalized)
        # Strip leading ./ if present
        parts = p.parts
        if parts and parts[0] == ".":
            parts = parts[1:]
        return str(PurePosixPath(*parts)) if parts else str(p)

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        """Match using fnmatch with ** support for recursive globbing."""
        # fnmatch doesn't natively handle **, so we handle it:
        # "src/genesis/channels/**" should match "src/genesis/channels/bridge.py"
        # and "src/genesis/channels/telegram/adapter.py"
        if "**" in pattern:
            # Convert ** to match any depth
            prefix = pattern.split("**")[0]
            if path.startswith(prefix):
                return True
            # Also try with the full fnmatch in case of trailing patterns
            # e.g., "**/*.service"
            import re
            regex_pattern = pattern.replace("**", "DOUBLESTAR")
            regex_pattern = fnmatch.translate(regex_pattern)
            regex_pattern = regex_pattern.replace("DOUBLESTAR", ".*")
            return bool(re.match(regex_pattern, path))
        return fnmatch.fnmatch(path, pattern)
