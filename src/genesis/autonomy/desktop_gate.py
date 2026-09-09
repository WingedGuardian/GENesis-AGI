"""Desktop-takeover authorization gate — the deterministic check that stands
between Genesis and the operator's own keyboard, mouse and screen.

This is the largest authority Genesis can hold: while a grant is live, anything
the operator can do at their machine, Genesis can do. The gate decides in CODE,
below the acting model. It is not a permissions prompt the model sees, and no
prose the model writes about its own intent reaches the classifier — the inputs
are the target the actuator RESOLVED (element name, control type, IsPassword
from the accessibility tree, plus the window title and any text to be typed).
Consent inferred from a model's self-report is exactly the weakness this exists
to avoid.

GROUNDWORK (desktop-takeover PR-2): nothing calls this yet, on purpose. The
actuator (PR-1) is inert and the loop, transport and MCP tool land in PR-3. An
actuator with a caller and no gate IS the ungated capability, so the gate ships
first and alone.

Four properties are load-bearing, each independently tested:

1. **Arming takes two keys.** ``mode: live`` AND ``live_opt_in: true``, neither
   reachable through ``settings_update`` or the dashboard. Default is
   ``shadow``: classify, record, refuse.
2. **Authority is per SESSION, MISSION and WINDOW, and must arrive from OUTSIDE
   this box.** A grant is an approved, unconsumed ``approval_requests`` row
   carrying this session's id, this mission's id, the target window's
   ``(handle, pid)`` and this module's ``kind``, resolved through
   :data:`DESKTOP_GRANT_RESOLVER_PREFIXES`.

   Every one of those is COMPARED, which is the point. The grant previously
   bound the window by TITLE and carried no mission at all, so two browser tabs
   both called "New Tab" shared one grant, a renamed document stopped matching
   its own window, and consent given for one mission covered any later one. A
   field carried and not compared is a promise the consent card makes and the
   code does not keep. That set is deliberately narrower
   than ``classify_resolver``'s "human" class: `dashboard` is stamped by a route
   any local process can reach with the internal token, and `user` is just a
   default. Neither proves a person acted, so neither can mint a grant here.
   A foreground CC conversation cannot mint one either — which is the point.
   The bar closes the APP-LAYER path (a Genesis component using the sanctioned
   approval APIs); it does not make a grant unforgeable by something with
   same-uid write access to the database, which no SQL predicate could. See
   :data:`DESKTOP_GRANT_RESOLVER_PREFIXES`.
3. **A capability cell can DENY desktop control but can never GRANT it.** The
   promotion path is a closed set (PR #1838) that does not contain ``desktop``,
   so no amount of banked evidence turns session consent into standing
   autonomy. The cell exists so the capability is visible in the matrix, and so
   the owner has a permanent off switch.
4. **A hold queues nothing.** Held desktop actions are not resumable: a desktop
   action aims at a screen that has since moved, so on approval the loop
   re-captures and re-plans. The invariant that buys is worth stating plainly —
   *every action executes against an observation taken after the last
   approval.*

Secret fields are outside all of that: ``is_password`` (or a target that merely
looks like a password field) is a REFUSAL with no approval path. There is no
version of this capability that types into a password box.

That last sentence is an absolute, so the thing that could falsify it is worth
naming: a KEY action has NO resolved target by construction — it acts on
whatever holds focus, which the gate cannot see — so ``is_password`` is always
False for a keypress and the secret-field refusal has nothing to match against.
The claim therefore rests on the inert-chord ALLOWLIST admitting no chord that
can INSERT content. ``ctrl+v`` was on that list and is not any more: ``tab`` is
inert, so the loop could reach a password box and paste into it, and the gate
would have called it ordinary input. What a keypress can still do to a focused
secret field is disturb it — backspace, undo — which is why the guarantee is
written as *types into* rather than *touches*.

TWO HONEST LIMITS, restated rather than quietly upgraded by this rewrite:

- **The grant predicate is NOT a complete authorization boundary.**
  ``genesis.db`` is a file writable by the uid every Genesis process runs as,
  so anything with same-uid code execution can INSERT a row satisfying every
  bar here. What the allowlist closes is the APP-LAYER path — a Genesis
  component using the sanctioned approval APIs can no longer mint itself
  desktop authority. Closing the rest needs provenance no SQL predicate can
  express. Do not let the loop in PR-3 be written believing otherwise.
- **The classifier is NOT a defence against adversarial UI.** IDENTITY and
  FINANCIAL are matchers over text the SCREEN supplies, and in this threat
  model the screen is hostile: a malicious page names its own controls and can
  label a destructive one "Continue". Adding the operation, the key chord and
  the reversibility verdict raises the bar on ORDINARY software — it does not
  make this a boundary against a page that is trying. The things that do hold
  there are the grant's own scope and the operator watching their screen.

AND A THIRD, which is new and belongs to this revision:

- **Approving a hold currently authorizes NOTHING.** The row is written, and
  no surface can resolve it: it is excluded from the dashboard queue, refused
  by ``resolve_request``, and excluded from ``approve_all_pending``, because
  desktop consent is meant to come from the purpose-built path that names the
  target window back to the operator — which lands with the loop in PR-3. The
  gate also never calls ``mark_consumed``. So the hold branch records an owner
  decision and stops there, on purpose: what an approved hold should BUY is a
  new authorization surface and needs the same binding discipline as the grant
  itself, and that decision belongs with the consent path that will carry it,
  not ahead of it. Stated here so the next reader finds a deliberate gap
  rather than what looks like a broken approve-then-act loop.
"""

# GROUNDWORK(desktop-takeover-pr2): this module has no call site on purpose.
# The actuator is inert and the loop/transport/MCP tool land in PR-3; the gate
# ships first so the dangerous combination (an actuator WITH a caller) never
# exists ungated. Do not delete as dead code.

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.autonomy.capabilities import InvalidTransition
from genesis.autonomy.classification import (
    OPERATIONS_REQUIRING_TARGET,
    DesktopAction,
    DesktopActionClassification,
    DesktopOperation,
    classify_desktop_action,
)
from genesis.autonomy.desktop_takeover_config import (
    action_ttl_seconds,
    effective_mode,
    grant_ttl_minutes,
)
from genesis.autonomy.types import CellEvent, CellState, RiskClass
from genesis.db.crud import approval_requests as ar
from genesis.db.crud import capability_grants as cg
from genesis.observability.types import Severity, Subsystem
from genesis.security.sanitizer import strip_control_chars

logger = logging.getLogger(__name__)

#: Distinct action_type so desktop holds and grants are isolated from every
#: batch-approval surface (see ``approve_all_pending``'s exclusion set) and from
#: the voice bare-"approve" resolver's allowlist.
DESKTOP_GATE_ACTION_TYPE = "desktop_takeover_gate"

#: ``context.kind`` marking an approval row that IS a session grant, as opposed
#: to a per-action hold. One home for the wire format: this gate READS it and
#: the PR-3 consent path WRITES it, so the two cannot drift apart.
#:
#: Both kinds share ONE action_type, deliberately — a single action_type is
#: what keeps every batch-approval exclusion to a single entry rather than a
#: set someone can half-update. The price is that `kind` is load-bearing: it is
#: the only thing distinguishing "the owner consented to this session" from
#: "the owner approved one click", and the grant lookup MUST filter on it.
SESSION_GRANT_KIND = "desktop_session_grant"

#: ``context.kind`` marking a per-action HOLD row.
#:
#: This exists because ``_hold`` used to write the ACTION_TYPE into the ``kind``
#: field — ``"desktop_takeover_gate"`` where every reader expects a kind. The
#: two strings are different, so the row was well-formed JSON carrying a value
#: no lookup would ever match: the hold path was WRITE-ONLY. It is a distinct
#: constant rather than a reuse so the grant lookup's ``kind`` filter keeps
#: meaning what it says.
DESKTOP_HOLD_KIND = "desktop_action_hold"

#: Wire-format version of a session-grant context blob.
#:
#: An UNKNOWN version is refused, not best-effort parsed. A grant is the
#: broadest authority here, and a blob written by a different version of the
#: consent path is a blob whose field meanings are not established — reading it
#: optimistically is how a field gets carried without being compared, which is
#: the defect class this whole rewrite exists to close.
SESSION_GRANT_VERSION = 1

#: Resolver prefixes that may mint a desktop SESSION GRANT. A deliberate
#: narrowing of :data:`HUMAN_RESOLVER_PREFIXES`, not a reuse of it.
#:
#: `classify_resolver` answers "was this written by a human-operated channel?",
#: which is the right question for metrics and the wrong one here. `dashboard`
#: is stamped unconditionally by the resolve route, and that route is reachable
#: by ANY local process holding the dashboard's internal bearer token — a file
#: readable by the uid every Genesis process runs as. So `dashboard` cannot
#: distinguish the owner from Genesis, and `user` is merely
#: `ApprovalManager.resolve`'s default, i.e. "nobody recorded who". Neither is
#: proof a person acted.
#:
#: What remains are channels whose MESSAGES originate outside this box: an
#: inbound Telegram callback from the owner's own account, and the voice
#: bridge's spoken resolution.
#:
#: What this bar does NOT do, stated plainly because the obvious reading of the
#: paragraph above is stronger than the truth: it does not make a grant
#: unforgeable. `resolved_by` is a column, and `genesis.db` is a file owned by
#: the uid every Genesis process runs as, so anything with same-uid code
#: execution can INSERT a row satisfying every bar here — including this one,
#: by simply typing an allowlisted prefix into it. That is a property of the
#: whole approval substrate (the autonomous-CLI gate and the email gate rest on
#: the same rows), not of this gate, and closing it needs provenance the SQL
#: predicate cannot express — a signature only the real resolver can produce, or
#: an OS-level identity split. What this allowlist DOES close is the app-layer
#: path: a Genesis component calling the sanctioned approval APIs — the
#: dashboard resolve route, an MCP tool, a background session — can no longer
#: mint itself desktop authority. Do not read it as more than that, and do not
#: let PR-3 wire a caller believing the predicate is a complete boundary.
#:
#: SETTLED 2026-09-07 by the owner, after a security review raised it: a spoken
#: challenge-response MAY open a session-length grant. `voice:` stays. Do not
#: re-raise it on ambient-audio or replay grounds.
#:
#: What that ruling does and does not cover. It settles the POLICY — voice is an
#: accepted consent channel for this capability. It does not assert that the
#: voice pipeline resists replayed or ambient audio, which is unverified and is
#: an engineering property, not a decision. The design already leans on the
#: right mitigation: the consent path NAMES the target window back to the
#: operator and waits for a specific answer, which is materially harder to
#: trigger by accident or replay than a bare "approve" — an attacker would need
#: the window name, and the grant is bounded, revocable and scoped to that one
#: window regardless. PR-3 should build the challenge-response as specified
#: rather than treating a naked "yes" as sufficient.
#:
#: Allowlist, not denylist — a resolver stays unable to grant desktop control
#: until someone decides otherwise in code. Pinned as a subset of
#: HUMAN_RESOLVER_PREFIXES by test, so the canonical mapping stays authoritative
#: and this can only ever be narrower.
DESKTOP_GRANT_RESOLVER_PREFIXES: tuple[str, ...] = ("telegram:", "voice:")


#: Bound on screen-supplied text where it reaches a HUMAN-FACING string. Window
#: titles and element names are conventionally short; a hostile page's are not,
#: and this text ends up in an approval card the owner reads to decide. This is
#: a SELECTION, not a loss — the full value is stored verbatim in the row's
#: context, so nothing is discarded, only the preview is bounded.
_DISPLAY_LIMIT = 120


def _display(value: str) -> str:
    """One-line, boundary-clean, bounded rendering of screen-supplied text.

    ``window_title`` / ``element_name`` come off the operator's screen, which
    this threat model treats as hostile. They flow into the approval row's
    ``description`` — the sentence a human reads before deciding — and into
    event-bus messages. Raw, they could carry newlines (forging extra lines in
    a rendered card), bidi overrides (reordering what is displayed away from
    what is approved) or zero-width concealment.

    ``strip_control_chars`` is the repo's canonical fix for exactly that class,
    derived from the Unicode database rather than hand-enumerated; the log
    calls in this module already get the same protection from ``%r``.
    """
    cleaned = strip_control_chars(value or "")
    if len(cleaned) <= _DISPLAY_LIMIT:
        return cleaned
    return f"{cleaned[:_DISPLAY_LIMIT]}… <{len(cleaned) - _DISPLAY_LIMIT} more chars>"


def build_session_grant_context(
    *,
    session_id: str,
    mission_id: str,
    mission: str,
    window_handle: str,
    process_id: int,
    window_title: str,
) -> dict[str, object]:
    """The ``context`` payload of a desktop session-grant approval row.

    Named here rather than at the future call site so the gate's reader and the
    PR-3 writer share one definition.

    Two of these fields are what the owner CONSENTS to and two are what the
    consent is COMPARED on, and conflating them was the original defect:

    - ``window_title`` and ``mission`` are for the consent card. The spoken
      challenge names the window back to the operator, which is what makes a
      "yes" specific enough to be consent rather than a reflex. Both are
      DISPLAY ONLY.
    - ``window_handle`` + ``process_id`` and ``mission_id`` are the identity the
      gate compares. A handle is reused after its window closes, so the pair is
      what names one live window.

    Why not compare the display fields. Two browser tabs are both called
    "New Tab", so a title bar authorises the wrong window while looking strict;
    and a title changes when the document inside it does, so the same window
    stops matching its own grant. Mission TEXT has the same defect one level up
    — it is model-generated and rewritten on every re-plan — which is why the
    id is what binds and the prose is what is shown.
    """
    return {
        "kind": SESSION_GRANT_KIND,
        "version": SESSION_GRANT_VERSION,
        "session_id": session_id,
        "mission_id": mission_id,
        "mission": mission,
        "window_handle": window_handle,
        "process_id": process_id,
        "window_title": window_title,
    }


@dataclass(frozen=True)
class SessionGrant:
    """A parsed, VALIDATED session grant. Constructing one is the validation.

    :meth:`parse` returns ``None`` rather than a partially-populated object, so
    a caller cannot hold something that looks like a grant but is missing the
    field it is about to compare. That shape is deliberate: the previous code
    passed the raw row around and each bar re-read the fields it happened to
    care about, which is how a field ends up carried but never compared.
    """

    row_id: str
    session_id: str
    mission_id: str
    window_handle: str
    process_id: int
    window_title: str
    mission: str
    resolved_by: str
    #: ``None`` when the stored timestamp is unparseable. The grant is still
    #: structurally a grant — it simply cannot be AGED, and the caller refuses
    #: it as expired. Kept nullable rather than refused at parse time so the
    #: refusal reason stays truthful: this is not "no grant from a human".
    resolved_at: datetime | None

    @classmethod
    def parse(cls, row: dict) -> SessionGrant | None:
        """Parse an approval row into a grant, or ``None`` if it is not one.

        Refuses: unreadable JSON, a non-mapping context, the wrong ``kind``, an
        unknown ``version``, a blank required field, a non-positive process id,
        and an unparseable ``resolved_at``. Every one of those is an UNBOUNDED
        grant if waved through — an un-ageable row never expires, and a blank
        field matches another blank field.
        """
        try:
            ctx = json.loads(row.get("context") or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(ctx, dict):
            return None
        if ctx.get("kind") != SESSION_GRANT_KIND:
            return None
        if ctx.get("version") != SESSION_GRANT_VERSION:
            logger.warning(
                "Desktop grant %s has unsupported context version %r — refusing",
                row.get("id"),
                ctx.get("version"),
            )
            return None

        # NOT a reason to return None. An unparseable timestamp is a real
        # refusal, but it is an AGEING failure, not a structural one — the row
        # is still recognisably this session's grant from an allowlisted
        # resolver. Folding it in here made the caller report
        # `grant_not_human` for a broken timestamp, sending whoever reads that
        # after the wrong thing entirely. The caller refuses it as expired,
        # which is what an un-ageable grant is.
        resolved_at = _parse_ts(row.get("resolved_at"))
        if resolved_at is None:
            logger.warning(
                "Desktop grant %s has unparseable resolved_at %r — refusing",
                row.get("id"),
                row.get("resolved_at"),
            )

        # `bool` is an int subclass, so `process_id: true` would otherwise
        # become pid 1 — a real pid on every Linux box.
        raw_pid = ctx.get("process_id")
        if isinstance(raw_pid, bool):
            return None
        try:
            process_id = int(raw_pid)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if process_id <= 0:
            return None

        session_id = str(ctx.get("session_id") or "").strip()
        mission_id = str(ctx.get("mission_id") or "").strip()
        window_handle = str(ctx.get("window_handle") or "").strip()
        # Blank is a REFUSAL, never a wildcard. In SQLite `'' = ''` is true, so
        # a grant with an empty session id authorises a caller with an empty
        # session id — the shape that looks safe because an ABSENT key extracts
        # NULL and never matches anything.
        if not (session_id and mission_id and window_handle):
            return None

        return cls(
            row_id=str(row.get("id") or ""),
            session_id=session_id,
            mission_id=mission_id,
            window_handle=window_handle,
            process_id=process_id,
            window_title=str(ctx.get("window_title") or ""),
            mission=str(ctx.get("mission") or ""),
            resolved_by=str(row.get("resolved_by") or ""),
            resolved_at=resolved_at,
        )

    def authorises(self, action: DesktopAction, *, session_id: str, mission_id: str) -> bool:
        """Whether this grant covers *action* for this session and mission.

        Every field compared here is one the consent card showed the operator.
        A field carried and NOT compared is a promise the card makes and the
        code does not keep, which is worse than not carrying it at all.

        BOTH SIDES ARE NORMALIZED, and they must be. ``parse`` stores a
        stripped handle and an ``int`` pid; the action side is whatever the
        caller constructed. An actuator handing back ``process_id="4312"`` —
        the natural shape out of JSON or a PowerShell bridge — passes
        validation (``int("4312") > 0``) and would then fail here forever,
        because ``4312 == "4312"`` is False. That is fail-CLOSED, so it is not
        a hole; it is worse than a hole to diagnose. The gate would report
        ``grant_scope_mismatch`` — "that grant was for a different window" —
        for a session whose grant is for exactly that window, sending whoever
        reads it to the consent path instead of the actuator. The refusal
        reason is the whole diagnostic payoff of per-window binding, so it must
        not be able to name the wrong bar.
        """
        return not self.mismatch(action, session_id=session_id, mission_id=mission_id)

    def mismatch(
        self, action: DesktopAction, *, session_id: str, mission_id: str
    ) -> str:
        """``""`` if this grant covers the action, else WHICH bar failed.

        Split out because folding three comparisons into one boolean made the
        caller report ``grant_scope_mismatch`` — "that grant was for a
        different window" — for a MISSION mismatch. This module's own standard
        is that a refusal reason must not be able to name the wrong bar, and it
        was breaking it here.
        """
        if self.session_id != str(session_id or "").strip():
            return "grant_session_mismatch"
        if self.mission_id != str(mission_id or "").strip():
            return "grant_mission_mismatch"
        if self.window_handle != str(action.window_handle or "").strip():
            return "grant_window_mismatch"
        if self.process_id != int(action.process_id):
            return "grant_window_mismatch"
        return ""


@dataclass(frozen=True)
class DesktopGateDecision:
    """Outcome of one desktop-gate check. ``allow`` False ⇒ nothing happened."""

    allow: bool
    #: Set on a HOLD — the approval row the owner must resolve. Nothing resumes
    #: it; the loop re-captures and re-plans on approval.
    request_id: str | None = None
    reason: str = ""
    #: (domain, verb, risk_class) of the cell this action classified into.
    cell: tuple[str, str, str] | None = None
    #: ISO-8601 UTC freshness stamp travelling with an allowed action. The
    #: DEVICE refuses a request past it (genesis-act.ps1), so an action that
    #: sat in transit dies at the machine rather than landing on a moved screen.
    expires_at: str | None = None
    #: The lever's mode at decision time (``off`` | ``shadow`` | ``live``).
    mode: str = ""
    #: Shadow only: what LIVE would have decided. Lets the posture be observed
    #: without ever acting, and makes "shadow refused" distinguishable from
    #: "the gate would have refused anyway".
    would_allow: bool = False


class DesktopTakeoverGate:
    """Deterministic owner-authorization gate for desktop input."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        approval_manager: object,
        event_bus: object | None = None,
    ) -> None:
        self._db = db
        self._approval = approval_manager
        self._event_bus = event_bus

    async def check(
        self,
        action: DesktopAction,
        *,
        session_id: str,
        mission_id: str,
    ) -> DesktopGateDecision:
        """Allow, hold or refuse one desktop input action.

        The action arrives as ONE validated object rather than a handful of
        optional strings. That is the substantive change: the previous
        signature could not carry the operation, the key chord or the window
        handle, so the gate could not tell Ctrl+Enter in a mail composer from
        typing a letter, and could not tell two windows both called "New Tab"
        apart. Those were not gaps in care — the information was not in the
        input type, and no amount of pattern-matching adds information that was
        never passed.

        A call that omits a required field is REFUSED with a reason naming the
        field, never held. A hold would ask the operator to approve an action
        nobody can describe — "something in some window" — and a missing
        required field is a CALLER bug, not a judgement to delegate. Nothing
        calls this gate yet, so that refusal costs nothing today and means the
        loop in PR-3 cannot be built wrong: it has to say what it is doing
        before the gate will act on it.

        The order below is the design, not an accident. Cheap total refusals
        come first; in LIVE mode the first DB WRITE happens only after the
        session grant has been verified, so an unauthorized caller cannot make
        the gate record anything on its behalf. Shadow deliberately writes the
        cell either way — observing is its whole job, and it can never act.
        Classification is pure and therefore runs early, so a refusal names the
        real reason instead of the first tripwire.
        """
        now = datetime.now(UTC)
        mode = effective_mode()

        # 1. Not armed at all — a refusal, never a hold. An unarmed capability
        #    must not queue work for the owner to approve later.
        if mode == "off":
            return DesktopGateDecision(allow=False, reason="not_armed", mode=mode)

        # 2. The call itself must be well-formed. Refused in BOTH modes with
        #    the real reason: shadow is the mode that actually ships, so it is
        #    where a malformed caller has to be visible. Reporting "shadow"
        #    here would hide the one class of defect shadow exists to surface.
        malformed = _validate_call(action, session_id=session_id, mission_id=mission_id)
        if malformed:
            logger.warning(
                "Desktop gate REFUSED a malformed call (%s, operation=%r, window=%r)",
                malformed,
                getattr(action.operation, "value", action.operation),
                action.window_title,
            )
            return DesktopGateDecision(allow=False, reason=malformed, mode=mode)

        # 3. Classify from the RESOLVED TARGET. Pure: no reads, no writes.
        classification = classify_desktop_action(action)
        domain, verb, risk = classification.cell_key
        window_title = action.window_title
        element_name = action.element_name

        # 4. Secret field — refused outright, before any cell exists for it.
        #    Not a hold: there is no approval that makes this acceptable, so
        #    the gate must not offer the owner a button that says otherwise.
        if classification.is_password:
            logger.warning(
                "Desktop gate REFUSED a password-field action (window=%r, element=%r)",
                window_title,
                element_name,
            )
            return DesktopGateDecision(
                allow=False, reason="password_field", cell=(domain, verb, risk), mode=mode
            )

        # 5. Session consent. Looked up in BOTH modes: shadow's whole job is
        #    to report what live would have decided, and "would it have had a
        #    grant?" is most of that answer.
        grant, grant_reason = await self._live_session_grant(
            action, session_id=session_id, mission_id=mission_id, now=now
        )

        # 6. Shadow observes and refuses. It records the cell and logs the full
        #    verdict — including a missing grant, which is the state a shadow
        #    install is actually IN, since nobody asks for keyboard consent for
        #    a capability that cannot act. An observer that only reports on
        #    sessions that already hold a grant observes nothing at all.
        #
        #    It creates no approval row: shadow must not put buttons in front of
        #    the owner for a capability that cannot act.
        #
        #    The verdict comes from the SAME function live uses. It used to be
        #    recomputed by hand here, and a hand-maintained copy of the live
        #    policy is worse than no shadow at all: shadow is the mode that
        #    actually ships, so a copy that silently disagrees misreports the
        #    posture of every install running it.
        if mode != "live":
            state = await self._classify_cell(domain, verb, risk, now)
            would_allow, reason = _verdict(
                classification=classification, grant=grant,
                grant_reason=grant_reason, cell_state=state,
            )
            logger.info(
                "Desktop gate SHADOW: would %s %s:%s:%s (window=%r, element=%r)",
                "allow" if would_allow else f"refuse ({reason})",
                domain,
                verb,
                risk,
                window_title,
                element_name,
            )
            return DesktopGateDecision(
                allow=False,
                reason="shadow",
                cell=(domain, verb, risk),
                mode=mode,
                would_allow=would_allow,
            )

        # ── live from here ───────────────────────────────────────────────
        # 7. No consent, no action. Nothing is written on this path: an
        #    unauthorized caller must not be able to make the gate record
        #    anything on its behalf. This is why the cell write below sits
        #    AFTER the grant check rather than beside it in _verdict.
        if grant is None:
            return DesktopGateDecision(
                allow=False, reason=grant_reason, cell=(domain, verb, risk), mode=mode
            )

        # 8. Make the cell visible in the matrix. The cell's only authority over
        #    this gate is NEGATIVE: it can never reach GRANTED (desktop is
        #    absent from PROMOTABLE_DOMAINS), so it is never a source of
        #    permission — but DENIED_PERMANENT is the owner's standing "not
        #    this, ever", and it outranks a live session grant.
        state = await self._classify_cell(domain, verb, risk, now)

        # 9. One policy, one place.
        allow, reason = _verdict(
            classification=classification, grant=grant,
            grant_reason=grant_reason, cell_state=state,
        )
        if not allow:
            if reason == "held":
                # Above STANDARD, session consent is not enough. Crossing the
                # identity bar or touching money is its own decision, every time.
                return await self._hold(
                    classification, action, session_id, mission_id, mode
                )
            logger.warning(
                "Desktop gate REFUSED %s:%s:%s — %s", domain, verb, risk, reason
            )
            return DesktopGateDecision(
                allow=False, reason=reason, cell=(domain, verb, risk), mode=mode
            )

        # 10. Ordinary input under a live grant — the ONE outcome that moves the
        #    operator's mouse, so it is also the one that must leave a trace.
        #    Everything else here logs its refusal; an allow that logged nothing
        #    would make the acted-upon case the only invisible one.
        # Also read fresh: stamping from the pre-await capture silently
        # shortens the window the DEVICE enforces, so an action could be
        # refused at the machine for time it was never given.
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=action_ttl_seconds())
        ).isoformat()
        logger.info(
            "Desktop gate ALLOWED %s:%s:%s on %r in %r (grant=%s, expires=%s)",
            domain,
            verb,
            risk,
            element_name,
            window_title,
            grant.row_id,
            expires_at,
        )
        await self._emit_allowed(window_title, element_name, grant.row_id)
        return DesktopGateDecision(
            allow=True,
            reason=reason,
            cell=(domain, verb, risk),
            expires_at=expires_at,
            mode=mode,
            would_allow=True,
        )

    async def _classify_cell(self, domain: str, verb: str, risk: str, now: datetime) -> CellState:
        """CLASSIFY the cell (NOT_DETERMINED -> ASK) and return its state.

        Suppresses InvalidTransition: the cell may already be past ASK, and a
        classify that cannot advance it is not an error.
        """
        with contextlib.suppress(InvalidTransition):
            await cg.apply_event(
                self._db,
                domain=domain,
                verb=verb,
                risk_class=risk,
                event=CellEvent.CLASSIFY,
                updated_at=now.isoformat(),
                # WS-3 gate-3: Genesis's own deterministic classifier.
                origin_class="first_party",
            )
        cell = await cg.get_cell(self._db, domain, verb, risk)
        return CellState(cell["state"]) if cell else CellState.ASK

    async def _live_session_grant(
        self,
        action: DesktopAction,
        *,
        session_id: str,
        mission_id: str,
        now: datetime,
    ) -> tuple[SessionGrant | None, str]:
        """The owner's live grant for this session, mission AND window.

        Eight independent bars, each of which has to hold. Every one exists
        because its absence was a defect found in review, not because it seemed
        prudent:

        - **this session** — the grant carries the session id and is filtered on
          it in SQL, so one session's consent is never another's. Compared HERE
          as well, because SQLite says ``'' = ''`` is true: a grant with a blank
          session id would otherwise authorise a caller with a blank one. An
          ABSENT key extracts NULL and never matches, which is exactly why this
          looks safe and is not;
        - **a GRANT, not a hold** — both are rows of the same action_type, so
          ``kind`` is what separates "consented to this session" from "approved
          one click". Without it, approving a single held action silently became
          a full session grant with a fresh expiry;
        - **a KNOWN wire format** — an unrecognised ``version`` is refused
          rather than read optimistically;
        - **this MISSION** — bound to ``mission_id``, never to the mission
          prose. The text is model-generated and is rewritten on every re-plan,
          so comparing it would reproduce the window-title failure one level up:
          consent given for one mission would stop matching the mission it was
          given for, while a differently-worded mission would slip through;
        - **this WINDOW** — ``(window_handle, process_id)``, not the title. Two
          browser tabs are both called "New Tab", so a title bar authorises the
          wrong window while looking strict; and a handle alone is reused after
          its window closes, so the PAIR is what names one live window. The
          title is still carried, for the consent card that names it back to
          the operator;
        - **approved and unconsumed** — consumption is how a session's grant is
          retired, so a consumed row is a finished session, not a live one;
        - **resolved through an allowlisted channel** —
          :data:`DESKTOP_GRANT_RESOLVER_PREFIXES`, deliberately narrower than
          ``classify_resolver``'s human class. `dashboard` and `user` are NOT
          proof a person acted (see the constant), so they cannot mint a grant
          even though they classify as human elsewhere;
        - **unexpired, bounded both ways** — ``resolved_at`` within the
          configured TTL and not in the future. A grant nobody remembers giving
          must lapse without needing a teardown to run, and a backwards clock
          step must not mint a permanent one.

        The refusal reason names the FIRST bar that failed rather than a generic
        denial, because the owner-facing difference between "you never granted
        this" and "that grant was for a different window" is the whole point of
        asking per window.
        """
        rows = await ar.list_approved_unconsumed_for_session(
            self._db,
            action_type=DESKTOP_GATE_ACTION_TYPE,
            session_id=session_id,
            # Without this the per-action HOLD rows this same gate writes would
            # satisfy the predicate, so approving one click would grant the
            # whole session.
            kind=SESSION_GRANT_KIND,
        )
        if not rows:
            return None, "no_session_grant"

        ttl = timedelta(minutes=grant_ttl_minutes())
        saw_parsed = False
        saw_allowlisted = False
        # The most specific near-miss seen, so the reason names the bar that
        # actually failed rather than a generic scope refusal.
        scope_reason = ""

        for row in rows:
            grant = SessionGrant.parse(row)
            # Unparseable, wrong kind, unknown version, or missing a field this
            # is about to compare. Not a grant, so not a near-miss either.
            if grant is None:
                continue
            saw_parsed = True
            if not grant.resolved_by.startswith(DESKTOP_GRANT_RESOLVER_PREFIXES):
                continue
            saw_allowlisted = True

            mismatch = grant.mismatch(
                action, session_id=session_id, mission_id=mission_id
            )
            if mismatch:
                scope_reason = scope_reason or mismatch
                continue

            if grant.resolved_at is None:
                # Un-ageable, so unbounded. Falls through to "expired", which
                # is what a grant that can never lapse has to be treated as.
                continue

            # Read the clock HERE, not from the caller's capture. `now`
            # was taken before `effective_mode()` read two config files
            # off disk and before this function's own SQL round trip; a
            # stale clock makes the grant look YOUNGER than it is, so a
            # grant that lapsed during that work would still authorize.
            # That is the fail-OPEN direction, on the one bar whose job
            # is to make a forgotten grant die on its own.
            age = datetime.now(UTC) - grant.resolved_at
            # Bounded on BOTH sides. A future-dated resolution gives a negative
            # age, which `age <= ttl` alone accepts forever — a backwards clock
            # step or a hand-edited row would mint a permanent grant.
            if timedelta(0) <= age <= ttl:
                return grant, "session_grant"

        # Ordered so the reason names the FIRST bar that failed. Each is a
        # different thing to go and fix, and reporting the wrong one sends the
        # reader after the wrong thing: "no grant from a human" and "the grant
        # blob is malformed" have nothing to do with each other.
        if not saw_parsed:
            return None, "grant_malformed"
        if not saw_allowlisted:
            return None, "grant_not_human"
        if scope_reason:
            return None, scope_reason
        return None, "grant_expired"

    async def _hold(
        self,
        classification: DesktopActionClassification,
        action: DesktopAction,
        session_id: str,
        mission_id: str,
        mode: str,
    ) -> DesktopGateDecision:
        """Record an owner decision for one above-STANDARD action.

        Deliberately queues NOTHING. There is no pending-action table and no
        drain: approving this row does not replay the action, because by then
        the screen it targeted has moved. The loop re-captures and re-plans.

        KNOWN GAP for PR-3, named here rather than left to be discovered: there
        is no dedup. Each call creates a fresh, never-expiring row, so a loop
        retrying the same blocked click every tick would leave one permanent
        approval per attempt. Harmless while nothing calls the gate; the loop
        that lands in PR-3 must either back off on a hold or reuse
        ``AutonomousCliApprovalGate._find_existing``'s content-stable key. (The
        email gate has the same shape, but one user-initiated send is not a
        stuck actuator loop.)
        """
        domain, verb, risk = classification.cell_key
        context = json.dumps(
            {
                # A KIND, not the action_type. This wrote
                # DESKTOP_GATE_ACTION_TYPE, which no reader ever looks for, so
                # the hold path was write-only: a row nothing could find.
                "kind": DESKTOP_HOLD_KIND,
                "cell": [domain, verb, risk],
                "session_id": session_id,
                # The IDENTITY triple, recorded for the same reason the grant
                # compares it: a row naming only a window TITLE cannot be tied
                # back to the window it was about, and the title is neither
                # unique nor stable. Written now, while the writer and the
                # reader are one file apart — whatever a later PR decides an
                # approved hold authorizes will need exactly these fields, and
                # rows written before then would be unbindable.
                "mission_id": mission_id,
                "window_handle": action.window_handle,
                "process_id": action.process_id,
                "operation": str(action.operation),
                "window_title": action.window_title,
                "element_name": action.element_name,
                "sub_class": classification.sub_class,
            }
        )
        request_id = await self._approval.request_approval(
            action_type=DESKTOP_GATE_ACTION_TYPE,
            action_class=str(classification.action_class),
            # NEWLINE-delimited, one field per line, and NOT quoted inline.
            # `_display` guarantees its output contains no newline (it runs
            # `strip_control_chars`), so a newline is the one delimiter screen
            # text cannot forge — whereas an apostrophe is not: an element
            # named `Cancel' in 'Notepad - untitled` rendered a card that read
            # as well-formed and named the WRONG window, demoting the real one
            # to a trailing fragment. That is the same forgery `_display`
            # exists to stop, reached with purely-printable text, which
            # `strip_control_chars` explicitly disclaims covering.
            #
            # Stripping quotes instead would have corrupted every legitimate
            # title ("Bob's Document"). Structure beats sanitisation here.
            description=(
                f"Desktop {classification.sub_class} action.\n"
                f"Control: {_display(action.element_name) or 'an unnamed control'}\n"
                f"Window: {_display(action.window_title) or 'an unnamed window'}\n"
                "Approving lets the session re-plan from a fresh capture; "
                "it does not replay this action."
            ),
            context=context,
            # Wait for the owner — never auto-approve, never auto-drop. This
            # holds only while DESKTOP_GATE_ACTION_TYPE is absent from the
            # timeout tables (a null timeout is what makes expire_timed_out
            # skip the row); test_desktop_gate pins that.
            timeout_seconds=None,
        )
        await self._emit_held(classification, action.window_title, action.element_name)
        logger.info(
            "Desktop gate HELD %s action (cell=%s:%s:%s, window=%r, request=%s)",
            classification.sub_class,
            domain,
            verb,
            risk,
            action.window_title,
            request_id,
        )
        return DesktopGateDecision(
            allow=False,
            request_id=request_id,
            reason="held",
            cell=(domain, verb, risk),
            mode=mode,
        )

    async def _emit_allowed(self, window_title: str, element_name: str, request_id: object) -> None:
        """Emit the audit event for the one outcome that actually acts."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.INFO,
                "autonomy.desktop_action_allowed",
                f"Desktop gate allowed an action on '{_display(element_name)}' "
                f"in '{_display(window_title)}' under session grant {request_id}",
            )
        except Exception:
            logger.error("Failed to emit autonomy.desktop_action_allowed", exc_info=True)

    async def _emit_held(
        self,
        classification: DesktopActionClassification,
        window_title: str,
        element_name: str,
    ) -> None:
        """Emit the EXPECTED autonomy.gate_held event at INFO (Tenet 0b) — a
        hold is routine autonomy behaviour, not a tool failure."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.INFO,
                "autonomy.gate_held",
                f"Desktop gate held a {classification.sub_class} action on "
                f"'{_display(element_name)}' in '{_display(window_title)}' "
                f"(cell {':'.join(classification.cell_key)})",
            )
        except Exception:
            logger.error("Failed to emit autonomy.gate_held", exc_info=True)


def _validate_call(
    action: DesktopAction, *, session_id: str, mission_id: str
) -> str:
    """``""`` if the call is well-formed, else the refusal reason.

    Every check here answers "did the caller tell us what it is doing?", and a
    "no" is a CALLER defect rather than a decision to put in front of the
    operator. Holding instead would ask them to approve an action nobody can
    describe.

    ``unresolved_target`` is the one worth naming out loud: a click with both
    ``element_name`` and ``control_type`` blank is a click at raw coordinates,
    which the classifier cannot see and the operator cannot be shown. It was
    previously authorised like any other ordinary action. It applies only to
    operations that HAVE a resolved target (see
    :data:`OPERATIONS_REQUIRING_TARGET`) — a key chord acts on whatever holds
    focus, and requiring a target it can never have would refuse every keypress.
    """
    if not str(session_id or "").strip():
        return "no_session_id"
    if not str(mission_id or "").strip():
        return "no_mission_id"

    # `==`-style membership via the enum constructor, never `is`: DesktopOperation
    # is a StrEnum, so a raw string that equals a member is a legitimate value
    # an identity check would reject.
    try:
        operation = DesktopOperation(action.operation)
    except ValueError:
        return "unknown_operation"

    if not str(action.window_handle or "").strip():
        return "malformed_action:window_handle"

    # `bool` is an int subclass, so `process_id=True` would pass an int check
    # and become pid 1.
    if isinstance(action.process_id, bool):
        return "malformed_action:process_id"
    try:
        process_id = int(action.process_id)
    except (TypeError, ValueError):
        return "malformed_action:process_id"
    if process_id <= 0:
        return "malformed_action:process_id"

    if operation == DesktopOperation.KEY and not str(action.key_chord or "").strip():
        return "malformed_action:key_chord"

    if operation in OPERATIONS_REQUIRING_TARGET and not (
        str(action.element_name or "").strip() or str(action.control_type or "").strip()
    ):
        return "unresolved_target"

    return ""


def _verdict(
    *,
    classification: DesktopActionClassification,
    grant: SessionGrant | None,
    grant_reason: str,
    cell_state: CellState,
) -> tuple[bool, str]:
    """The whole allow/hold/refuse policy, in ONE place. Pure.

    This function is the deliverable of the shadow/live de-duplication. Shadow
    used to recompute the live verdict by hand — three terms that had to be
    kept in step with the live path by whoever edited it next. Every bar added
    to the gate meant editing the policy twice, and a shadow that silently
    disagrees with live is worse than no shadow, because shadow is the mode
    installs actually run: it is the only thing reporting what live WOULD do.

    ``"held"`` is a verdict, not an outcome — the caller decides whether that
    means writing an approval row (live) or simply reporting it (shadow).
    """
    if grant is None:
        return False, grant_reason
    if cell_state == CellState.DENIED_PERMANENT:
        return False, "denied_permanent"
    # `!=`, never `is not`: RiskClass is a StrEnum (see classification.py).
    if classification.risk_class != RiskClass.STANDARD:
        return False, "held"
    return True, "session_grant"


def _parse_ts(raw: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp as UTC-aware, or None.

    Every in-tree writer of ``resolved_at`` passes an aware UTC value
    (``ApprovalManager.resolve``), so the naive branch is defensive rather than
    a live inconsistency — it covers a hand-edited row or a restored backup,
    where UTC is still the only sane reading. An UNPARSEABLE value returns None
    and the caller refuses: an un-ageable grant is an unbounded one.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
