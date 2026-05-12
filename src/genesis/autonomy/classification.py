"""Irreversibility classification — maps (action_class, autonomy_level) to approval decisions.

V3 logic is intentionally conservative:
  - REVERSIBLE → ACT (always)
  - COSTLY_REVERSIBLE → PROPOSE (always)
  - IRREVERSIBLE → PROPOSE (always, no exceptions)

Internally delegates to the data-driven :class:`RuleEngine` from
``config/autonomy_rules.yaml``. Falls back to hard-coded V3 defaults
if the rules file is missing or malformed. The lookup table will become
level-sensitive in V4 when earned autonomy unlocks per-task exemptions.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from genesis.autonomy.rules import RuleContext, RuleEngine
from genesis.autonomy.types import ActionClass, ApprovalDecision

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "autonomy.yaml"
_DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "autonomy_rules.yaml"

# ---------------------------------------------------------------------------
# V3 defaults — used when config AND rules are both missing or malformed
# ---------------------------------------------------------------------------

_DEFAULT_APPROVAL_POLICY: dict[str, str] = {
    "reversible": "act",
    "costly_reversible": "propose",
    "irreversible": "propose",
}

_DEFAULT_APPROVAL_TIMEOUTS: dict[str, int | None] = {
    "outreach": None,
    "task_proposal": None,
    "autonomous_cli_fallback": None,
    "sentinel_dispatch": None,
    "sentinel_action": None,
    "irreversible": None,
}

# ---------------------------------------------------------------------------
# Keyword patterns for classify_action() hint function
# ---------------------------------------------------------------------------

_IRREVERSIBLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:delete|pay|submit|purchase|remove\s+account)\b", re.IGNORECASE),
]

_COSTLY_REVERSIBLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:send|push|post|publish|message|email)\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# ActionClassifier
# ---------------------------------------------------------------------------


class ActionClassifier:
    """Maps (ActionClass, autonomy_level) to an ApprovalDecision.

    Loads rules from ``autonomy_rules.yaml`` via :class:`RuleEngine`.
    Falls back to the policy dict in ``autonomy.yaml`` (or hard-coded V3
    defaults) if the rules file is unavailable.

    Also loads approval timeouts from ``autonomy.yaml``.
    """

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        rules_path: Path | None = None,
    ) -> None:
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._rules_path = rules_path or _DEFAULT_RULES_PATH

        # Timeouts loaded from autonomy.yaml (unchanged)
        self._approval_timeouts: dict[str, int | None] = dict(_DEFAULT_APPROVAL_TIMEOUTS)

        # Fallback policy from autonomy.yaml (used when rules engine fails)
        self._approval_policy: dict[str, str] = dict(_DEFAULT_APPROVAL_POLICY)

        # Load config (timeouts + fallback policy)
        self._load_config()

        # Initialize rule engine
        self._rule_engine: RuleEngine | None = None
        self._init_rule_engine()

    # -- public API --------------------------------------------------------

    def classify(self, action_class: ActionClass, autonomy_level: int) -> ApprovalDecision:
        """Return the approval decision for *action_class* at *autonomy_level*.

        Delegates to the rule engine if available, otherwise falls back
        to the policy dict from autonomy.yaml / V3 defaults.
        """
        if self._rule_engine is not None:
            ctx = RuleContext(
                action_class=action_class,
                autonomy_level=autonomy_level,
            )
            result = self._rule_engine.evaluate(ctx)
            return result.decision

        # Fallback: policy dict lookup
        return self._classify_fallback(action_class)

    def is_approval_required(self, action_class: ActionClass, autonomy_level: int) -> bool:
        """Return ``True`` if the action requires user approval (PROPOSE or BLOCK)."""
        decision = self.classify(action_class, autonomy_level)
        return decision in (ApprovalDecision.PROPOSE, ApprovalDecision.BLOCK)

    def get_timeout(self, action_type: str) -> int | None:
        """Return the approval timeout in seconds for *action_type*.

        Returns ``None`` for action types that should wait indefinitely
        (e.g. irreversible actions).
        """
        return self._approval_timeouts.get(action_type)

    # -- rule engine -------------------------------------------------------

    def _init_rule_engine(self) -> None:
        """Initialize the rule engine from autonomy_rules.yaml."""
        try:
            engine = RuleEngine(rules_path=self._rules_path)
            if engine.rule_count > 0:
                self._rule_engine = engine
                logger.debug(
                    "ActionClassifier using RuleEngine (%d rules)",
                    engine.rule_count,
                )
            else:
                logger.debug(
                    "RuleEngine loaded 0 rules — using fallback policy",
                )
        except Exception:
            logger.warning(
                "Failed to initialize RuleEngine — using fallback policy",
                exc_info=True,
            )

    # -- fallback (original V3 logic) --------------------------------------

    def _classify_fallback(self, action_class: ActionClass) -> ApprovalDecision:
        """Fallback classification using policy dict from autonomy.yaml."""
        key = action_class.value
        raw = self._approval_policy.get(key)
        if raw is None:
            logger.warning(
                "No approval policy for action class %r — defaulting to PROPOSE",
                key,
            )
            return ApprovalDecision.PROPOSE

        try:
            return ApprovalDecision(raw)
        except ValueError:
            logger.error(
                "Invalid approval decision %r for action class %r — defaulting to PROPOSE",
                raw,
                key,
            )
            return ApprovalDecision.PROPOSE

    # -- config loading (timeouts + fallback policy) -----------------------

    def _load_config(self) -> None:
        """Load approval_policy and approval_timeouts from autonomy.yaml."""
        if not self._config_path.exists():
            logger.warning(
                "Autonomy config not found at %s — using V3 defaults",
                self._config_path,
            )
            return

        try:
            raw_text = self._config_path.read_text(encoding="utf-8")
            data: Any = yaml.safe_load(raw_text)
        except (OSError, yaml.YAMLError):
            logger.error(
                "Failed to load autonomy config from %s — using V3 defaults",
                self._config_path,
                exc_info=True,
            )
            return

        if not isinstance(data, dict):
            logger.warning(
                "Autonomy config is not a mapping — using V3 defaults",
            )
            return

        # approval_policy (fallback only — primary path is RuleEngine)
        policy = data.get("approval_policy")
        if isinstance(policy, dict):
            self._approval_policy = {str(k): str(v) for k, v in policy.items()}
        elif policy is not None:
            logger.warning("approval_policy is not a mapping — using defaults")

        # approval_timeouts
        timeouts = data.get("approval_timeouts")
        if isinstance(timeouts, dict):
            parsed: dict[str, int | None] = {}
            for k, v in timeouts.items():
                if v is None:
                    parsed[str(k)] = None
                else:
                    try:
                        parsed[str(k)] = int(v)
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid timeout value %r for %r — skipping",
                            v,
                            k,
                        )
            self._approval_timeouts.update(parsed)
        elif timeouts is not None:
            logger.warning("approval_timeouts is not a mapping — using defaults")


# ---------------------------------------------------------------------------
# Standalone hint function
# ---------------------------------------------------------------------------


def classify_action(action_description: str) -> ActionClass:
    """Classify an action description into an :class:`ActionClass` via keyword matching.

    This is a *hint* function — callers can override the result when they
    have better domain knowledge.  The patterns are intentionally simple;
    the LLM layer is expected to refine classification where needed.
    """
    # Check irreversible first (more restrictive wins)
    for pattern in _IRREVERSIBLE_PATTERNS:
        if pattern.search(action_description):
            return ActionClass.IRREVERSIBLE

    for pattern in _COSTLY_REVERSIBLE_PATTERNS:
        if pattern.search(action_description):
            return ActionClass.COSTLY_REVERSIBLE

    return ActionClass.REVERSIBLE
