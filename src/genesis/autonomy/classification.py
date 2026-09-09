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
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from genesis.autonomy.rules import RuleContext, RuleEngine
from genesis.autonomy.types import (
    ActionClass,
    ActionDomain,
    ApprovalDecision,
    RiskClass,
    max_risk,
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
                    continue
                # FOUR guards, matching `_coerce_pid` and `_positive_int`. The
                # first pass here took only OverflowError — the least
                # consequential of them — while claiming the class was
                # enumerated. This is the widest site of the three: the
                # classifier is shared with the email gate and is the default
                # timeout source for EVERY approval type, so a bad value here
                # reaches far more than one gate.
                #
                #   bool: `autonomous_cli_fallback: true` -> int(True) == 1, a
                #     ONE-SECOND window on the owner's approval, expired by the
                #     60s poller before any channel renders it.
                #   non-integral float: int() TRUNCATES rather than refusing.
                #   <= 0: a timeout already in the past, same outcome as bool.
                #   OverflowError: `.inf` is valid YAML; unlike its sibling
                #     this branch IS reachable, there being no float guard above
                #     it that catches infinity first.
                if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
                    logger.warning("Invalid timeout value %r for %r — skipping", v, k)
                    continue
                try:
                    seconds = int(v)
                except (TypeError, ValueError, OverflowError):
                    logger.warning("Invalid timeout value %r for %r — skipping", v, k)
                    continue
                if seconds <= 0:
                    logger.warning("Non-positive timeout %r for %r — skipping", v, k)
                    continue
                parsed[str(k)] = seconds
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
    # Plurals are matched explicitly. `\bpayment\b` does NOT match "Payments",
    # because the trailing "s" defeats the word boundary — and "Payments" is
    # what a real banking window is actually called. MEASURED while checking
    # whether a money label backstops the IBAN checksum: a window titled
    # "Chase - Payments" classified STANDARD, while "Online Banking" did not.
    # The singular-only list was quietly missing the commonest spelling.
    re.compile(
        r"\b(?:wire[\s-]*transfer|bank|banking|payments?|pay\s+now|checkout|"
        r"card\s*numbers?|cvv|cvc|iban|routing\s*numbers?|account\s*numbers?|"
        r"invoices?|remit|transfer\s+funds|place\s+order|confirm\s+purchase|"
        r"billing)\b",
        re.IGNORECASE,
    ),
    # SHAPES — what the CONTENT is. A label-only list made "FINANCIAL also reads
    # the typed text" an empty promise: an actual card number typed into a field
    # labelled "Confirmation" matched nothing and passed as STANDARD, so the
    # claim and the code disagreed. These catch the value itself.
    #   * 13 OR MORE digits, optionally grouped (card / long account numbers)
    #   * IBAN shape (country + 2 check digits + 11-30 alphanumerics)
    #   * US SSN shape
    # Deliberately fail-CLOSED: a long digit run that is not a card number costs
    # one hold, and a hold is the safe direction for money.
    #
    # NO UPPER BOUND. `{13,19}` combined with the trailing `(?!\d)` did not mean
    # "13 to 19 digits" — it meant a run of exactly 13-19 with no digit after
    # it, so a run of TWENTY matched nothing at all and classified STANDARD.
    # A cap on the high side of a fail-closed check is an evasion, not a bound:
    # padding a card number with one extra digit walked straight through it.
    re.compile(r"(?<!\d)(?:\d[ -]?){13,}(?!\d)"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
]

#: SHAPE of an IBAN — a candidate finder, NOT the test. Deliberately loose
#: (case-insensitive, separators anywhere) because the checksum below is what
#: decides; a loose shape with a real validator beats a tight shape guessing.
#:
#: ``re.ASCII`` is load-bearing, not tidiness. Under ``IGNORECASE`` alone,
#: ``[A-Z]`` matches non-ASCII codepoints through Unicode case folding —
#: U+212A KELVIN SIGN case-folds to "k" and U+0130 (İ) to "i". Both are
#: ``str.isalpha()`` and both survive ``.upper()``, so they reach
#: ``int(ch, 36)`` in the checksum and raise ValueError. That exception escapes
#: the classifier and the gate's ``check()`` entirely, and the text it comes
#: from is a WINDOW TITLE — screen-supplied, which this module's threat model
#: treats as hostile. Enumerated over all 0x110000 codepoints: exactly those
#: two break the consumer, and ``re.ASCII`` excludes both.
_IBAN_SHAPE = re.compile(
    r"\b[A-Z][ -]?[A-Z][ -]?\d[ -]?\d(?:[ -]?[A-Z0-9]){10,30}\b",
    re.IGNORECASE | re.ASCII,
)


def _iban_checksum_ok(candidate: str) -> bool:
    """ISO 7064 mod-97: move the first four characters to the end, map letters
    to 10-35, and read the result as an integer congruent to 1 mod 97.

    This is the DEFINING property of an IBAN, and using it instead of a shape
    heuristic is what keeps the pattern from holding on ordinary text. The
    alternative — constraining the country code to the real registry — was
    measured and does NOT work: `de`, `be`, `ad`, `ae`, `ba` and `ee` are all
    real IBAN prefixes AND valid leading hex pairs, so roughly 1 in 110 git
    SHAs matched. A commit hash is not a bank transfer, and a gate that holds
    on one teaches the operator to approve without reading.
    """
    compact = re.sub(r"[ -]", "", candidate).upper()
    # Belt to _IBAN_SHAPE's braces, and deliberately duplicated: the pattern and
    # this function will be edited by different people at different times, and
    # either one alone is enough to feed `int(ch, 36)` a character it raises on.
    # `isalpha()`/`isdigit()` are Unicode-wide and are NOT guards here —
    # `str.isdigit()` is True for superscripts that `int()` rejects.
    if not compact.isascii():
        return False
    if not (15 <= len(compact) <= 34) or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    if not digits.isdigit():
        return False
    return int(digits) % 97 == 1


def _contains_iban(text: str) -> bool:
    """True when *text* holds something that is actually an IBAN.

    An IBAN is written lowercase as often as not, and its human-facing form is
    grouped in fours ("DE89 3704 0044 0532 0130 00"). The previous
    uppercase-and-contiguous-only pattern matched neither spelling.

    MEASURED, and smaller than it looks: over ISO 13616 specimens in all three
    spellings, this recovers 2/24 that no other financial pattern caught — the
    letter-heavy ones (Malta), whose BBAN breaks the digit run below the card
    pattern's 13-digit floor. Every IBAN with a long digit run was already
    FINANCIAL via that pattern, spelling notwithstanding. Kept because the
    letter-heavy cases are real and the checksum makes them nearly free.
    """
    return any(_iban_checksum_ok(m.group(0)) for m in _IBAN_SHAPE.finditer(text))

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
        r"\b(?:password|passphrase|passcode|passkey|otp|"
        r"one[\s-]*time\s*(?:code|password)|2fa|mfa|"
        r"(?:verification|security|auth(?:entication)?|recovery)\s*(?:code|key|"
        r"question|answer)|secret\s*(?:key|answer)|seed\s*phrase|"
        r"social\s*security|ssn)\b",
        re.IGNORECASE,
    ),
    # "pin" needs its own rule, because a bare case-insensitive `\bpin\b` makes
    # "Pin to taskbar" and "Pin to Start" — ordinary Windows shell actions —
    # match as SECRET FIELDS. That is the worst possible false positive here:
    # a secret-field match is a REFUSAL with no approval path, so the operator
    # cannot proceed at all, and the reason names something the control has
    # nothing to do with. MEASURED before this split: element_name
    # "Pin to taskbar" returned is_password=True.
    #
    # So the credential senses are matched explicitly instead: PIN with a
    # credential noun after it, or with a credential verb/qualifier before it.
    re.compile(
        r"\bpin\s*(?:code|number|entry)\b"
        r"|\b(?:enter|confirm|new|current|old|your)\s+pin\b",
        re.IGNORECASE,
    ),
    # And bare "PIN", CASE-SENSITIVELY. Uppercase is the credential spelling
    # ("PIN"); the shell verb is "Pin"/"unpin". This is a heuristic on a
    # BACKSTOP list — the accessibility IsPassword flag is the primary signal,
    # and this list exists only because that flag is unreliable on custom
    # controls. It is deliberately not IGNORECASE.
    re.compile(r"\bPIN\b"),
]


class DesktopOperation(StrEnum):
    """What the actuator is about to DO — the half of an action a label cannot say.

    A control's name describes the thing being operated; it says nothing about
    the operating. Clicking "Message" and typing into "Message" are the same
    label and different acts, and Ctrl+Enter in a mail composer sends the mail
    while producing no element name at all. Without this field the classifier
    was answering a question it had not been asked.

    A CLOSED set, and unknown values are refused rather than defaulted: a new
    operation nobody has reasoned about is the one case where guessing is
    guaranteed wrong, because whoever added it knows something this table does
    not.
    """

    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    DRAG = "drag"


#: Operations whose meaning is carried by a NAMED control, so a blank target is
#: a blind action at coordinates rather than a narrower one. Excluded:
#: ``KEY`` and ``SCROLL`` act on whatever holds focus (there is no element to
#: resolve), and ``DRAG`` acts on geometry — which is why it carries its own
#: floor below instead of a target requirement it could never satisfy.
OPERATIONS_REQUIRING_TARGET: frozenset[DesktopOperation] = frozenset(
    {
        DesktopOperation.CLICK,
        DesktopOperation.DOUBLE_CLICK,
        DesktopOperation.RIGHT_CLICK,
        DesktopOperation.TYPE,
    }
)

#: Risk floor contributed by the operation itself, independent of any label.
#:
#: Everything that operates a RESOLVED control sits at STANDARD, because the
#: control's own name is what carries its consequence and the pattern lists
#: already read it — a second opinion here would double-count. ``KEY`` is also
#: STANDARD because the risk lives in the CHORD, not in the fact of a keypress
#: (see :func:`key_chord_risk`).
#:
#: ``DRAG`` is the exception and the reason this table exists: a drag acts on
#: coordinates, so no element name describes its effect. Dropping a folder onto
#: another folder moves it, and the accessibility tree reports the same thing it
#: reports for dragging a scrollbar. Nothing downstream can tell those apart, so
#: the operation is held rather than guessed at. Drags are rare in an automation
#: loop, so the friction this buys is small and the blind spot it closes is not.
OPERATION_RISK: dict[DesktopOperation, RiskClass] = {
    DesktopOperation.CLICK: RiskClass.STANDARD,
    DesktopOperation.DOUBLE_CLICK: RiskClass.STANDARD,
    DesktopOperation.RIGHT_CLICK: RiskClass.STANDARD,
    DesktopOperation.TYPE: RiskClass.STANDARD,
    DesktopOperation.KEY: RiskClass.STANDARD,
    DesktopOperation.SCROLL: RiskClass.STANDARD,
    DesktopOperation.DRAG: RiskClass.IDENTITY,
}

#: Risk floor contributed by the reversibility classifier's verdict.
#:
#: This is what closes the "``Pay`` classifies STANDARD" hole. ``Pay`` and
#: ``Remove account`` match neither the financial label list (which wants
#: ``pay now`` / ``payment``) nor the identity list, yet
#: :func:`classify_action` has always called both IRREVERSIBLE. Two classifiers
#: disagreed about the same control and nothing reconciled them.
#:
#: COSTLY_REVERSIBLE deliberately stays STANDARD. Its keywords are
#: ``send|push|post|publish|message|email``, and the text this is computed over
#: includes the WINDOW TITLE — so promoting it would make every click inside a
#: window called "Messages" an identity action. That is the hold-fatigue
#: failure, not a safety gain; those verbs are already in the identity list
#: where they name an actual control.
_ACTION_CLASS_RISK: dict[ActionClass, RiskClass] = {
    ActionClass.REVERSIBLE: RiskClass.STANDARD,
    ActionClass.COSTLY_REVERSIBLE: RiskClass.STANDARD,
    ActionClass.IRREVERSIBLE: RiskClass.IDENTITY,
}

#: Modifier spellings folded to one canonical name before a chord is compared.
_CHORD_MODIFIER_ALIASES: dict[str, str] = {
    "ctrl": "ctrl",
    "ctl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "opt": "alt",
    "option": "alt",
    "shift": "shift",
    "meta": "meta",
    "win": "meta",
    "cmd": "meta",
    "command": "meta",
    "super": "meta",
}

#: Chords that only NAVIGATE or EDIT TEXT — an ALLOWLIST, and the polarity is
#: the whole point. A denylist of dangerous chords has misses that are
#: vulnerabilities (nobody lists every send-shortcut in every application);
#: this list's misses are one hold apiece.
#:
#: THE ADMISSION CRITERION IS "CANNOT INSERT CONTENT INTO UNSEEN FOCUS".
#: A KEY action has no resolved target by construction — it acts on whatever
#: holds focus, which the gate cannot see and the accessibility tree is not
#: consulted about. So ``is_password`` is always False for a keypress, and the
#: module's absolute "nothing types into a password box" survives only if no
#: allowlisted chord can put content there.
#:
#: Absent on purpose, each for a stated reason:
#:   * ``enter`` / ``space`` — activate whatever holds focus. They are a click
#:     by another name, and the gate cannot see what has focus.
#:   * ``delete`` — deletes a character in a text field and a FILE in a file
#:     manager. Identical keystroke, and nothing here distinguishes them.
#:   * ``ctrl+x`` — marks a file-manager selection for a MOVE (see below).
#:   * ``ctrl+v`` — INSERTS the clipboard into whatever holds focus. ``tab`` is
#:     on this list, so the loop can reach a password box and paste into it,
#:     and no target exists for the secret-field refusal to match against.
#:     That is not the stated "hostile screen" limit — there is no control name
#:     to have lied. MEASURED before removal: KEY/``ctrl+v`` under a neutral
#:     window title classified STANDARD and was ALLOWED.
#:   * anything with ``meta`` — the Windows key reaches the shell, which is
#:     outside the window the grant is scoped to.
#:
#: ``backspace``, ``ctrl+z`` and ``ctrl+y`` ARE here, and the line is drawn at
#: INSERTION rather than at mutation: they can delete or revert characters in a
#: focused field, but they cannot put chosen content into one, so they cannot
#: fill a credential box. Holding them would break ordinary typing correction.
#: ``ctrl+c`` reads rather than writes. A residual limit worth naming: a
#: keypress can still reach a focused secret field and disturb it. The
#: guarantee is about typing INTO one, and is stated that way at the top of
#: desktop_gate.py.
_INERT_KEY_CHORDS: frozenset[str] = frozenset(
    {
        # Caret movement and selection.
        "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
        "shift+up", "shift+down", "shift+left", "shift+right",
        "shift+home", "shift+end",
        "ctrl+left", "ctrl+right", "ctrl+home", "ctrl+end",
        "ctrl+shift+left", "ctrl+shift+right", "ctrl+shift+home", "ctrl+shift+end",
        # Focus movement and dismissal.
        "tab", "shift+tab", "escape",
        # Text editing and the clipboard. Neither ``ctrl+x`` nor ``ctrl+v``:
        # cut marks a file-manager selection for a MOVE and paste completes it,
        # and paste is separately the one chord that can fill a focused
        # password box. Removing the pair breaks the move chain and closes the
        # insertion route in one go; copy, undo and redo cannot place chosen
        # content anywhere.
        "backspace", "ctrl+a", "ctrl+c",
        "ctrl+z", "ctrl+y", "ctrl+shift+z",
        # Save and find — ubiquitous, and neither commits anything outward.
        "ctrl+s", "ctrl+f",
    }
)


def normalize_key_chord(chord: str) -> str:
    """Fold a key chord to one canonical spelling for comparison.

    ``Ctrl+Shift+Z``, ``shift+control+z`` and ``CTRL + SHIFT + z`` are the same
    keystroke and must not be three different allowlist misses. Modifiers are
    aliased and sorted; the base key goes last. Returns ``""`` for empty input.

    A BLANK SEGMENT makes the chord unreadable, and this must not quietly drop
    it. Normalization feeds an allowlist, so any collapse runs toward "inert" —
    and ``"ctrl+a+ "`` (a literal space key, which is how pyautogui's ``hotkey``
    spells space) would otherwise normalize onto the allowlisted ``ctrl+a``,
    even though SPACE is deliberately absent from the list because it activates
    whatever holds focus. The module's own rule applies one layer down: missing
    information must not be able to look like empty information.
    """
    raw = str(chord or "").split("+")
    parts = [p.strip().lower() for p in raw]
    if not parts or all(not p for p in parts):
        # Genuinely empty input — distinct from a chord WITH a blank segment.
        # The gate refuses this earlier as `malformed_action:key_chord`.
        return ""
    if any(not p for p in parts):
        # Unreadable, not shorter. A sentinel that can never be in the
        # allowlist, so key_chord_risk lands on IDENTITY.
        return "\x00unreadable"
    mods = sorted({_CHORD_MODIFIER_ALIASES[p] for p in parts if p in _CHORD_MODIFIER_ALIASES})
    keys = sorted(p for p in parts if p not in _CHORD_MODIFIER_ALIASES)
    # A bare modifier press ("shift") has no base key; the modifiers ARE the
    # chord. Joining the empty key list would otherwise produce a trailing "+".
    return "+".join([*mods, *keys]) if keys else "+".join(mods)


def key_chord_risk(chord: str) -> RiskClass:
    """STANDARD for a chord known to be inert, IDENTITY for everything else.

    An unrecognised chord is held rather than allowed, which is the allowlist
    doing its job: ``ctrl+enter`` sends mail in most clients and appears on no
    denylist anyone finished writing.
    """
    normalized = normalize_key_chord(chord)
    if not normalized:
        # A KEY action with no chord is unresolvable. The gate refuses this
        # earlier as a malformed call; the floor here means a caller that
        # bypasses the gate still cannot get an empty chord waved through.
        return RiskClass.IDENTITY
    return RiskClass.STANDARD if normalized in _INERT_KEY_CHORDS else RiskClass.IDENTITY


@dataclass(frozen=True)
class DesktopAction:
    """One desktop input action, as the actuator RESOLVED it.

    Every field the gate needs to decide, in one object, with the required ones
    carrying NO DEFAULTS on purpose. The gate this replaces took keyword
    arguments that all defaulted to ``""``, so a caller could omit the window
    it was acting in and be answered anyway — the missing information looked
    exactly like empty information, and there is no way to tell those apart
    after the fact. A required field with no default makes the omission a
    call-site error at construction, which is where it can still be fixed.

    ``window_handle`` and ``process_id`` travel together because a window
    handle is reused after its window closes; the pair is what identifies one
    live window. ``window_title`` is carried for the CONSENT CARD — the
    sentence naming the window back to the operator — and is deliberately not
    what authority is compared on, because a title changes when the document
    inside it does.
    """

    operation: DesktopOperation
    window_handle: str
    process_id: int
    #: A token the ACTUATOR guarantees changes when the window it identifies is
    #: destroyed and another takes its place.
    #:
    #: Required because `(handle, pid)` is not enough, and the reason is the
    #: same one that disqualified the title: it is not unique OVER TIME. A
    #: Windows HWND is valid for a window's lifetime and is then RECYCLED, and
    #: the pid does not save it — a browser or editor keeps one process alive
    #: across many windows, so a newly created window in that process can
    #: receive the closed window's handle and inherit its still-live grant.
    #: A 30-minute grant is long enough for that in an application that opens
    #: and closes windows.
    #:
    #: The gate does not care HOW it is produced — only that the actuator, which
    #: owns the resolve step, mints a fresh value whenever it resolves a window
    #: it has not seen alive continuously. A per-resolution GUID invalidated when
    #: `IsWindow` goes false is the obvious construction. This is stated as a
    #: CONTRACT rather than computed here because the gate cannot observe window
    #: lifetimes; requiring the information is the whole design of this module.
    window_nonce: str
    window_title: str
    element_name: str
    control_type: str
    #: Required when ``operation`` is ``KEY``; ignored otherwise.
    key_chord: str = ""
    #: Text to be typed. Read by the FINANCIAL patterns only — see
    #: :func:`classify_desktop_action` for why it never reaches the identity bar.
    text: str = ""
    #: The accessibility tree's IsPassword flag, where the control exposed one.
    is_password: bool = False


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


#: Per-field ceiling on screen-supplied text before it reaches the patterns.
#:
#: A SAFETY bound, so it is derived from the threat and the runtime rather than
#: from observed values. Classification is synchronous work inside an ``async``
#: gate that runs once per action: MEASURED at roughly 1.2 ms per KB across all
#: pattern groups (which are linear — there is no catastrophic backtracking
#: here), so an accessibility tree exposing a document's full contents as an
#: element name would block the whole event loop for minutes. 8 KiB is orders
#: of magnitude above any real window title or control name and still bounds
#: the work at ~10 ms per field.
#:
#: This cap is LOSSY on purpose and that is the one case the "never truncate"
#: rule allows — an unbounded adversarial input with no other guard. So it is
#: LOUD: it logs when it bites, naming the field and both sizes, because a
#: silent cut here would mean a card number past the boundary reads as absent.
_CLASSIFIER_FIELD_LIMIT = 8192


def _bounded(value: str, field: str) -> str:
    """Screen text, bounded for the classifier. Never silently.

    Coerces first. These fields come off an actuator payload, and a JSON number
    arriving where a string was declared reached `len()` as an int and raised
    TypeError out of the classifier. The gate refuses non-string metadata at its
    boundary; this is the second layer, because the classifier is also called
    directly by tests and by whatever comes after PR-3.
    """
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if len(text) <= _CLASSIFIER_FIELD_LIMIT:
        return text
    logger.warning(
        "Desktop classifier bounded %s: %d chars supplied, %d classified — "
        "content past the bound was NOT examined",
        field,
        len(text),
        _CLASSIFIER_FIELD_LIMIT,
    )
    return text[:_CLASSIFIER_FIELD_LIMIT]


def classify_desktop_action(action: DesktopAction) -> DesktopActionClassification:
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

    FOUR INDEPENDENT SIGNALS, COMBINED HIGHEST-WINS. The label patterns are one
    of them, not the answer:

    1. the pattern verdict over the control (and, for money, the typed text);
    2. the reversibility classifier's verdict — which catches ``Pay`` and
       ``Remove account``, on neither desktop list;
    3. the operation itself, for ``DRAG``, whose effect no label describes;
    4. the key chord, for ``KEY`` — because ``ctrl+enter`` sends the mail and
       produces no element name at all.

    Each was a hole while risk came from patterns alone, and the holes are not
    the same shape, which is why one more pattern was never going to close them.
    """
    element_name = _bounded(action.element_name, "element_name")
    control_type = _bounded(action.control_type, "control_type")
    window_title = _bounded(action.window_title, "window_title")
    text_value = _bounded(action.text, "text")

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
    haystack = f"{control}\n{text_value}"

    password = bool(action.is_password) or any(
        p.search(target) for p in _PASSWORD_TARGET_PATTERNS
    )

    if any(p.search(haystack) for p in _FINANCIAL_DESKTOP_PATTERNS) or _contains_iban(haystack):
        pattern_risk, pattern_sub = RiskClass.FINANCIAL, "financial"
    elif any(p.search(control) for p in _IDENTITY_DESKTOP_PATTERNS):
        pattern_risk, pattern_sub = RiskClass.IDENTITY, "identity"
    else:
        pattern_risk, pattern_sub = RiskClass.STANDARD, "input"

    # `control`, NOT `haystack`. Reading the typed text here would make
    # `_IRREVERSIBLE_PATTERNS` (which matches a bare "delete") classify the act
    # of TYPING "please delete the old draft" into a plain editor as an
    # irreversible action, and the highest-wins rule below would then hold it.
    # That is the precise protection the identity branch above was built to
    # give — it reads `control` and never `haystack` for the same reason — and
    # it is pinned by test_typing_an_identity_word_is_not_an_identity_action.
    # Money keeps its content channel regardless: _FINANCIAL_DESKTOP_PATTERNS
    # read `haystack` independently, two branches up.
    action_class = classify_action(control)

    # Ordered: a pattern verdict wins ties, so a control the label lists already
    # name still reports "identity" rather than the operation that agreed with
    # them. The label is the most specific thing we know about the action.
    signals: list[tuple[RiskClass, str]] = [
        (pattern_risk, pattern_sub),
        (_ACTION_CLASS_RISK.get(action_class, RiskClass.IDENTITY), "irreversible"),
        (OPERATION_RISK.get(action.operation, RiskClass.IDENTITY), f"operation:{action.operation}"),
    ]
    # `==`, never `is`: DesktopOperation is a StrEnum, so identity comparison
    # silently disagrees with equality the moment a raw string reaches here
    # (the defect PR #1838 fixed one module over).
    if action.operation == DesktopOperation.KEY:
        signals.append((key_chord_risk(action.key_chord), "key_chord"))

    risk = max_risk(*(r for r, _ in signals))
    # The first signal that reached the winning risk — so the approval card can
    # say WHY it held, which is most of what makes a hold answerable.
    sub = next(label for r, label in signals if r == risk)

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
        action_class=action_class,
    )
