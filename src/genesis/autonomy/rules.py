"""Data-driven rule engine for autonomy decisions.

Evaluates rules from ``config/autonomy_rules.yaml`` against a context
to produce approval decisions. Rules are evaluated top-to-bottom;
first full match wins. If no rule matches, the default is ACT.

This replaces the hardcoded policy lookup in classification.py with
a data-driven approach that can be extended without code changes.

See docs/architecture/enforcement-spectrum.md for the enforcement taxonomy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from genesis.autonomy.types import (
    ActionClass,
    ApprovalDecision,
    ProtectionLevel,
)

logger = logging.getLogger(__name__)

_DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "config" / "autonomy_rules.yaml"
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleContext:
    """Context for rule evaluation — carries all the facts about an action."""

    action_class: ActionClass | None = None
    protection_level: ProtectionLevel | None = None
    autonomy_level: int | None = None
    context_category: str | None = None  # relay, background, direct, sub_agent
    description: str = ""


@dataclass(frozen=True)
class RuleResult:
    """Outcome of rule evaluation."""

    decision: ApprovalDecision
    rule_id: str
    timeout_seconds: int | None = None
    description: str = ""


@dataclass
class _ParsedRule:
    """Internal representation of a rule from YAML."""

    rule_id: str
    condition: dict[str, Any]
    decision: ApprovalDecision
    timeout_seconds: int | None = None
    description: str = ""


# Default result when no rule matches
_DEFAULT_RESULT = RuleResult(
    decision=ApprovalDecision.ACT,
    rule_id="_default",
    description="No rule matched — default to ACT.",
)


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


class RuleEngine:
    """Evaluate data-driven autonomy rules from YAML.

    Rules are loaded once on construction. Call :meth:`reload` to pick up
    changes without restarting.
    """

    def __init__(self, rules_path: Path | None = None) -> None:
        self._rules_path = rules_path or _DEFAULT_RULES_PATH
        self._rules: list[_ParsedRule] = []
        self._load()

    # -- public API --------------------------------------------------------

    def evaluate(self, ctx: RuleContext) -> RuleResult:
        """Evaluate *ctx* against rules. First full match wins."""
        for rule in self._rules:
            if self._matches(rule, ctx):
                return RuleResult(
                    decision=rule.decision,
                    rule_id=rule.rule_id,
                    timeout_seconds=rule.timeout_seconds,
                    description=rule.description,
                )
        return _DEFAULT_RESULT

    def reload(self) -> None:
        """Re-read rules from YAML. Safe to call at runtime.

        Uses atomic swap to avoid a window where rules list is empty.
        """
        self._rules = []
        self._load()

    @property
    def rule_count(self) -> int:
        """Number of loaded rules."""
        return len(self._rules)

    # -- matching ----------------------------------------------------------

    @staticmethod
    def _matches(rule: _ParsedRule, ctx: RuleContext) -> bool:
        """Check if all condition fields match the context (AND logic).

        Missing condition fields are wildcards (always match).
        Missing context fields never match a non-wildcard condition.
        """
        cond = rule.condition

        # action_class
        if "action_class" in cond:
            if ctx.action_class is None:
                return False
            if ctx.action_class.value != cond["action_class"]:
                return False

        # protection_level
        if "protection_level" in cond:
            if ctx.protection_level is None:
                return False
            if ctx.protection_level.value != cond["protection_level"]:
                return False

        # context — list of allowed contexts (match if ctx.context_category is in the list)
        if "context" in cond:
            allowed = cond["context"]
            if isinstance(allowed, str):
                allowed = [allowed]
            if ctx.context_category is None:
                return False
            if ctx.context_category not in allowed:
                return False

        # autonomy_level — exact match or range (for V4)
        if "autonomy_level" in cond:
            if ctx.autonomy_level is None:
                return False
            if ctx.autonomy_level != cond["autonomy_level"]:
                return False

        return True

    # -- loading -----------------------------------------------------------

    # Known condition keys — warn on unknown keys to catch typos
    _KNOWN_CONDITION_KEYS = frozenset({
        "action_class", "protection_level", "context", "autonomy_level",
    })

    def _load(self) -> None:
        """Load and validate rules from YAML."""
        if not self._rules_path.exists():
            logger.warning(
                "Autonomy rules not found at %s — no rules loaded",
                self._rules_path,
            )
            return

        try:
            raw = yaml.safe_load(self._rules_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            logger.error(
                "Failed to load autonomy rules from %s",
                self._rules_path,
                exc_info=True,
            )
            return

        if not isinstance(raw, dict) or "rules" not in raw:
            logger.warning("Autonomy rules YAML has no 'rules' key — no rules loaded")
            return

        rules_list = raw["rules"]
        if not isinstance(rules_list, list):
            logger.warning("Autonomy rules 'rules' key is not a list — no rules loaded")
            return

        for i, entry in enumerate(rules_list):
            if not isinstance(entry, dict):
                logger.warning("Rule #%d is not a mapping — skipping", i)
                continue

            rule_id = entry.get("rule_id", f"_unnamed_{i}")
            decision_str = entry.get("decision", "act")

            try:
                decision = ApprovalDecision(decision_str)
            except ValueError:
                logger.error(
                    "Rule '%s' has invalid decision '%s' — skipping",
                    rule_id,
                    decision_str,
                )
                continue

            timeout = entry.get("timeout_seconds")
            if timeout is not None:
                try:
                    timeout = int(timeout)
                except (TypeError, ValueError):
                    logger.warning(
                        "Rule '%s' has invalid timeout '%s' — using None",
                        rule_id,
                        timeout,
                    )
                    timeout = None

            # Normalize condition: null/missing -> empty dict
            condition = entry.get("condition") or {}

            # Warn on unknown condition keys (catches typos)
            unknown_keys = set(condition) - self._KNOWN_CONDITION_KEYS
            if unknown_keys:
                logger.warning(
                    "Rule '%s' has unknown condition keys %s — they will be ignored",
                    rule_id,
                    unknown_keys,
                )

            self._rules.append(_ParsedRule(
                rule_id=rule_id,
                condition=condition,
                decision=decision,
                timeout_seconds=timeout,
                description=entry.get("description", ""),
            ))

        logger.debug("Loaded %d autonomy rules from %s", len(self._rules), self._rules_path)
