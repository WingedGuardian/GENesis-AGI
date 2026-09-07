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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from genesis.autonomy.rules import RuleContext, RuleEngine
from genesis.autonomy.types import (
    ActionClass,
    ActionDomain,
    ApprovalDecision,
    RiskClass,
)

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
    "build_greenlight": None,
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


# ---------------------------------------------------------------------------
# Action domain classification (parallel taxonomy)
# ---------------------------------------------------------------------------

# Maps ego proposal action_type → ActionDomain.
# The ego outputs action_type naturally; the domain is derived externally
# so the ego never sees or reasons about the domain vocabulary.
ACTION_TYPE_DOMAIN_MAP: dict[str, ActionDomain] = {
    "investigate": ActionDomain.EXTERNAL_READ,
    "research": ActionDomain.EXTERNAL_READ,
    "analyze": ActionDomain.EXTERNAL_READ,
    "diagnose": ActionDomain.OBSERVE,
    "monitor": ActionDomain.OBSERVE,
    "maintenance": ActionDomain.INTERNAL_WRITE,
    "config": ActionDomain.INTERNAL_WRITE,
    "optimize": ActionDomain.INTERNAL_WRITE,
    "outreach": ActionDomain.REPRESENT_USER,
    "email": ActionDomain.REPRESENT_USER,
    "apply": ActionDomain.REPRESENT_USER,
    "notification": ActionDomain.NOTIFY_USER,
    "alert": ActionDomain.NOTIFY_USER,
    "publish": ActionDomain.EXTERNAL_WRITE,
    "content": ActionDomain.EXTERNAL_WRITE,
    "post": ActionDomain.EXTERNAL_WRITE,
    "purchase": ActionDomain.FINANCIAL,
    "payment": ActionDomain.FINANCIAL,
    "code_change": ActionDomain.SELF_MODIFY,
    "refactor": ActionDomain.SELF_MODIFY,
    # Build-lane capability builds land on a scope-gated branch as a draft PR
    # (never merged autonomously) — gated at level 2, not hard-blocked like
    # SELF_MODIFY, which changes running code/config in place.
    "autonomous_build": ActionDomain.AUTONOMOUS_BUILD,
    # Evo promotion rewrites Genesis's own deep-reflection prompt — a change to
    # its cognition. SELF_MODIFY is hard-blocked from background dispatch
    # (ACTION_DOMAIN_MIN_LEVEL = None), so an approved promotion can never be
    # auto-run as a session; it is applied ONLY by its resolution handler.
    "cognitive_variant_promotion": ActionDomain.SELF_MODIFY,
    # J-9 regression surfacing is INFORMATIONAL — it notifies the operator about
    # a cognitive-quality regression and applies nothing. NOTIFY_USER + the
    # _NEVER_DISPATCH_ACTION_TYPES blocklist mean an approved one is marked
    # executed by its handler, never auto-run as a background session.
    "j9_regression": ActionDomain.NOTIFY_USER,
}


def classify_domain(action_type: str, execution_plan: str = "") -> ActionDomain:
    """Derive ActionDomain from an ego proposal's action_type field.

    Primary source: ACTION_TYPE_DOMAIN_MAP lookup.
    Secondary: keyword heuristic on execution_plan (tools/URLs mentioned).
    Fallback: EXTERNAL_READ (safe default for most ego proposals).

    The ego never sees or outputs ActionDomain — this is derived externally
    to maintain opacity of the autonomy mechanism.
    """
    # Normalize
    action_type_lower = action_type.lower().strip()

    # Direct mapping
    if action_type_lower in ACTION_TYPE_DOMAIN_MAP:
        return ACTION_TYPE_DOMAIN_MAP[action_type_lower]

    # Check execution_plan for tool hints
    if execution_plan:
        plan_lower = execution_plan.lower()
        if any(kw in plan_lower for kw in ("browser_fill", "form", "apply", "submit")):
            return ActionDomain.REPRESENT_USER
        if any(kw in plan_lower for kw in ("outreach_send", "email", "linkedin")):
            return ActionDomain.REPRESENT_USER
        if any(kw in plan_lower for kw in ("publish", "medium", "post")):
            return ActionDomain.EXTERNAL_WRITE
        if any(kw in plan_lower for kw in ("payment", "purchase", "pay", "stripe")):
            return ActionDomain.FINANCIAL
        # SELF_MODIFY: must reference source code paths specifically.
        # Exclude ~/.genesis/ (output dir) and github URLs to avoid false positives.
        if (any(kw in plan_lower for kw in ("edit", "write", "modify"))
                and "src/genesis/" in plan_lower):
            return ActionDomain.SELF_MODIFY

    return ActionDomain.EXTERNAL_READ


# ---------------------------------------------------------------------------
# Email action classification (WS-8 capability matrix)
# ---------------------------------------------------------------------------
# Maps an outbound email action to its capability-cell key
# (domain="email", verb="send", risk_class) plus the irreversibility class.
# Like classify_domain, this is derived EXTERNALLY — the acting model never
# sees or reasons about the cell taxonomy (Tenet 0 opacity).  DARK in PR-B:
# the gate that calls this lives at the outreach_send chokepoint (PR-C), where
# thread/recipient context is in scope.

EMAIL_DOMAIN = "email"
EMAIL_VERB = "send"

_FINANCIAL_EMAIL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:wire\s+transfer|invoice|payment|remit|deposit|ach|iban|"
        r"routing\s+number|bank\s+details)\b",
        re.IGNORECASE,
    ),
]


@dataclass(frozen=True)
class EmailActionClassification:
    """Classification of one outbound email action for the capability gate."""

    domain: str                 # channel-domain for the cell key (always "email" here)
    verb: str                   # cell verb (always "send" here)
    risk_class: RiskClass       # cell risk axis
    sub_class: str              # human label: reply | cold | bulk | financial
    identity_bar: bool          # True = acts in the user's name (REPRESENT_USER)
    action_class: ActionClass   # reversibility (reused keyword classifier)

    @property
    def cell_key(self) -> tuple[str, str, str]:
        """The (domain, verb, risk_class) tuple used to look up the cell."""
        return (self.domain, self.verb, str(self.risk_class))


def classify_email_action(
    *,
    is_reply: bool = False,
    recipient_known: bool = False,
    is_bulk: bool = False,
    subject: str = "",
    body: str = "",
) -> EmailActionClassification:
    """Derive the capability-cell key for an outbound email.

    Risk gradient (low → high): a known-thread reply (STANDARD) < cold
    outreach to a new party, which crosses the identity bar (IDENTITY) <
    a bulk/campaign send (BULK).  Any monetary email is FINANCIAL (hardline).
    Inputs are supplied by the caller at the send chokepoint (PR-C).
    """
    text = f"{subject}\n{body}"
    if any(p.search(text) for p in _FINANCIAL_EMAIL_PATTERNS):
        risk, sub = RiskClass.FINANCIAL, "financial"
    elif is_bulk:
        risk, sub = RiskClass.BULK, "bulk"
    elif is_reply and recipient_known:
        risk, sub = RiskClass.STANDARD, "reply"
    else:
        risk, sub = RiskClass.IDENTITY, "cold"

    return EmailActionClassification(
        domain=EMAIL_DOMAIN,
        verb=EMAIL_VERB,
        risk_class=risk,
        sub_class=sub,
        identity_bar=True,  # email always represents the user (REPRESENT_USER)
        action_class=classify_action(text),
    )


# ---------------------------------------------------------------------------
# Desktop action classification (desktop-takeover capability gate)
# ---------------------------------------------------------------------------
# Maps one desktop input action to its capability-cell key
# (domain="desktop", verb="control", risk_class) plus the irreversibility class.
# Like classify_email_action this is derived EXTERNALLY, in CODE — but the
# inputs matter more here than anywhere else in the file.
#
# THE INPUTS ARE THE RESOLVED TARGET, NEVER THE ACTING MODEL'S PROSE ABOUT ITS
# OWN INTENT. element_name / control_type / is_password come from the machine's
# accessibility tree as the actuator resolved it, and window_title from the
# window it resolved against. A loop that means to click "Save" and resolves
# "Delete account" is classified on what it resolved. Consent derived from a
# model's self-report is the weakness this whole gate exists to avoid.
#
# KNOWN LIMIT, and larger than "a click with no metadata". IDENTITY and
# FINANCIAL are keyword matchers over text the SCREEN supplies, and in this
# threat model the screen is hostile: a malicious page controls its own window
# title, element names and control types. It can label a destructive control
# "Yes", "OK" or "Continue", use a synonym, use another language, or split a
# matched phrase — and the action then classifies STANDARD and executes under a
# live grant with no hold. So this classifier RAISES the bar on ordinary
# software; it is NOT a boundary against adversarial UI. The boundaries that do
# hold against that are the session grant itself (bounded, revocable, scoped to
# one window) and the operator watching their own screen.

DESKTOP_DOMAIN = "desktop"
DESKTOP_VERB = "control"

_FINANCIAL_DESKTOP_PATTERNS: list[re.Pattern[str]] = [
    # Labels — what the control CALLS itself.
    re.compile(
        r"\b(?:wire[\s-]*transfer|bank|banking|payment|pay\s+now|checkout|"
        r"card\s*number|cvv|cvc|iban|routing\s*number|account\s*number|invoice|"
        r"remit|transfer\s+funds|place\s+order|confirm\s+purchase|billing)\b",
        re.IGNORECASE,
    ),
    # SHAPES — what the CONTENT is. A label-only list made "FINANCIAL also reads
    # the typed text" an empty promise: an actual card number typed into a field
    # labelled "Confirmation" matched nothing and passed as STANDARD, so the
    # claim and the code disagreed. These catch the value itself.
    #   * 13-19 digits, optionally grouped (card / long account numbers)
    #   * IBAN shape (2 letters, 2 check digits, 11-30 alphanumerics)
    #   * US SSN shape
    # Deliberately fail-CLOSED: a long digit run that is not a card number costs
    # one hold, and a hold is the safe direction for money.
    re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
]

# Crossing the identity bar: the action speaks or acts AS the operator, or is
# not undoable by a second click. Deliberately broader than the reversibility
# keyword list — "reply" is reversible in no meaningful sense once it is sent.
_IDENTITY_DESKTOP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:send|post|publish|submit|reply|reply\s+all|forward|tweet|"
        r"delete|discard|purchase|buy|confirm|accept|sign|share|install|"
        r"uninstall|format|shut\s*down|restart|"
        # Account creation acts in the operator's name as much as a send does,
        # and was previously held only by accident — when the button happened
        # to read "Submit" or "Sign up". "Create" alone is deliberately NOT
        # here: it matches "Create folder"/"New document", which are ordinary.
        r"create\s+account|new\s+account|sign\s*up|register)\b",
        re.IGNORECASE,
    ),
]

# Fail-closed password detection independent of the accessibility flag. Matched
# against the RESOLVED TARGET only (element name + control type), never the
# window title: a window called "Sign in" must not make every action in it
# unreachable, but an element called "Password" must be untouchable regardless
# of whether the control exposed IsPassword. Custom controls routinely do not.
# For anything NOT on this list the accessibility flag is the only backstop —
# and the reason this list exists at all is that the flag is unreliable. So it
# covers the secret-field FAMILY, not just the word "password": one-time codes
# and security answers are lower-stakes than a standing password but are still
# credentials, and an SSN is not lower-stakes at all.
_PASSWORD_TARGET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:password|passphrase|passcode|passkey|pin(?:\s*code)?|otp|"
        r"one[\s-]*time\s*(?:code|password)|2fa|mfa|"
        r"(?:verification|security|auth(?:entication)?|recovery)\s*(?:code|key|"
        r"question|answer)|secret\s*(?:key|answer)|seed\s*phrase|"
        r"social\s*security|ssn)\b",
        re.IGNORECASE,
    ),
]


@dataclass(frozen=True)
class DesktopActionClassification:
    """Classification of one desktop input action for the takeover gate."""

    domain: str                 # cell-key domain (always "desktop" here)
    verb: str                   # cell verb (always "control" here)
    risk_class: RiskClass       # cell risk axis
    sub_class: str              # human label: input | identity | financial | password
    is_password: bool           # target is a secret field — refused outright
    identity_bar: bool          # True = acts in the operator's name
    action_class: ActionClass   # reversibility (reused keyword classifier)

    @property
    def cell_key(self) -> tuple[str, str, str]:
        """The (domain, verb, risk_class) tuple used to look up the cell."""
        return (self.domain, self.verb, str(self.risk_class))


def classify_desktop_action(
    *,
    window_title: str = "",
    element_name: str = "",
    control_type: str = "",
    is_password: bool = False,
    text: str = "",
) -> DesktopActionClassification:
    """Derive the capability-cell key for one desktop input action.

    Risk gradient (low - high): manipulating an ordinary control (STANDARD) <
    an action that acts in the operator's name or cannot be taken back
    (IDENTITY) < anything monetary (FINANCIAL, hardline). A secret field is not
    on that gradient at all: ``is_password`` is a REFUSAL flag with no approval
    path, so it is reported separately and the gate never offers to ask.

    Which input feeds which bar is a decision, not an oversight. IDENTITY reads
    the CONTROL (window + element + control type) because that is what performs
    the act; FINANCIAL additionally reads ``text``, because a card number is
    dangerous as content and not only as a label; and ``is_password`` matches
    the resolved TARGET alone, never the window, so a window called "Sign in"
    does not make every control inside it unreachable.
    """
    target = f"{element_name}\n{control_type}"
    # The CONTROL being operated, plus the window it lives in. Deliberately
    # excludes `text`: what the operator is typing is not what the action DOES.
    # Folding it in makes "please send me the file" typed into a plain editor
    # classify IDENTITY, so the most ordinary desktop action there is would
    # hold — a gate that stops the common case teaches people to wave it
    # through. Clicking "Send" is the identity act; typing the word is not.
    control = f"{window_title}\n{target}"
    # Money is the exception, and it is the right one: a card number or an IBAN
    # is dangerous as CONTENT, not only as a button label.
    haystack = f"{control}\n{text}"

    password = bool(is_password) or any(p.search(target) for p in _PASSWORD_TARGET_PATTERNS)

    if any(p.search(haystack) for p in _FINANCIAL_DESKTOP_PATTERNS):
        risk, sub = RiskClass.FINANCIAL, "financial"
    elif any(p.search(control) for p in _IDENTITY_DESKTOP_PATTERNS):
        risk, sub = RiskClass.IDENTITY, "identity"
    else:
        risk, sub = RiskClass.STANDARD, "input"

    return DesktopActionClassification(
        domain=DESKTOP_DOMAIN,
        verb=DESKTOP_VERB,
        risk_class=risk,
        sub_class="password" if password else sub,
        is_password=password,
        # `!=`, never `is not`: RiskClass is a StrEnum, so identity comparison
        # silently disagrees with equality the moment a raw string reaches here
        # (the defect PR #1838 fixed one module over).
        identity_bar=risk != RiskClass.STANDARD,
        action_class=classify_action(haystack),
    )
