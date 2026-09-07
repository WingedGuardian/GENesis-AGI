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
2. **Authority is per SESSION, and must arrive from OUTSIDE this box.** A grant
   is an approved, unconsumed ``approval_requests`` row carrying this session's
   id and this module's ``kind``, resolved through
   :data:`DESKTOP_GRANT_RESOLVER_PREFIXES`. That set is deliberately narrower
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
    DesktopActionClassification,
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
    window_title: str,
    mission: str,
) -> dict[str, str]:
    """The ``context`` payload of a desktop session-grant approval row.

    Named here rather than at the future call site so the gate's reader and the
    PR-3 writer share one definition. ``window_title`` and ``mission`` are the
    two things the owner is actually consenting to — the spoken challenge in
    PR-3 names the window back to them, which is what makes a "yes" specific
    enough to be consent rather than a reflex.
    """
    return {
        "kind": SESSION_GRANT_KIND,
        "session_id": session_id,
        "window_title": window_title,
        "mission": mission,
    }


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
        *,
        session_id: str,
        window_title: str = "",
        element_name: str = "",
        control_type: str = "",
        is_password: bool = False,
        text: str = "",
    ) -> DesktopGateDecision:
        """Allow, hold or refuse one desktop input action.

        The order below is the design, not an accident. Cheap total refusals
        come first; in LIVE mode the first DB WRITE happens only after the
        session grant has been verified, so an unauthorized caller cannot make
        the gate record anything on its behalf. Shadow deliberately writes the
        cell either way — observing is its whole job, and it can never act. Classification is pure and therefore runs early,
        so a refusal names the real reason instead of the first tripwire.
        """
        now = datetime.now(UTC)
        mode = effective_mode()

        # 1. Not armed at all — a refusal, never a hold. An unarmed capability
        #    must not queue work for the owner to approve later.
        if mode == "off":
            return DesktopGateDecision(allow=False, reason="not_armed", mode=mode)

        # 2. Classify from the RESOLVED TARGET. Pure: no reads, no writes.
        classification = classify_desktop_action(
            window_title=window_title,
            element_name=element_name,
            control_type=control_type,
            is_password=is_password,
            text=text,
        )
        domain, verb, risk = classification.cell_key

        # 3. Secret field — refused outright, before any cell exists for it.
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

        # 4. Session consent. Looked up in BOTH modes: shadow's whole job is
        #    to report what live would have decided, and "would it have had a
        #    grant?" is most of that answer.
        grant, grant_reason = await self._live_session_grant(
            session_id, window_title, now
        )

        # 5. Shadow observes and refuses. It records the cell and logs the full
        #    verdict — including a missing grant, which is the state a shadow
        #    install is actually IN, since nobody asks for keyboard consent for
        #    a capability that cannot act. An observer that only reports on
        #    sessions that already hold a grant observes nothing at all.
        #
        #    It creates no approval row: shadow must not put buttons in front of
        #    the owner for a capability that cannot act.
        if mode != "live":
            state = await self._classify_cell(domain, verb, risk, now)
            would_allow = (
                grant is not None
                and classification.risk_class == RiskClass.STANDARD
                and state != CellState.DENIED_PERMANENT
            )
            if would_allow:
                verdict = "allow"
            elif grant is None:
                verdict = f"refuse ({grant_reason})"
            elif state == CellState.DENIED_PERMANENT:
                verdict = "refuse (denied_permanent)"
            else:
                verdict = "hold"
            logger.info(
                "Desktop gate SHADOW: would %s %s:%s:%s (window=%r, element=%r)",
                verdict,
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
        # 6. No consent, no action. Nothing is written on this path: an
        #    unauthorized caller must not be able to make the gate record
        #    anything on its behalf.
        if grant is None:
            return DesktopGateDecision(
                allow=False, reason=grant_reason, cell=(domain, verb, risk), mode=mode
            )

        # 7. Make the cell visible in the matrix. The cell's only authority over
        #    this gate is NEGATIVE: it can never reach GRANTED (desktop is
        #    absent from PROMOTABLE_DOMAINS), so it is never a source of
        #    permission — but DENIED_PERMANENT is the owner's standing "not
        #    this, ever", and it outranks a live session grant.
        state = await self._classify_cell(domain, verb, risk, now)
        if state == CellState.DENIED_PERMANENT:
            logger.warning(
                "Desktop gate REFUSED %s:%s:%s — cell denied permanently", domain, verb, risk
            )
            return DesktopGateDecision(
                allow=False, reason="denied_permanent", cell=(domain, verb, risk), mode=mode
            )

        # 8. Above STANDARD, session consent is not enough. Crossing the
        #    identity bar or touching money is its own decision, every time.
        if classification.risk_class != RiskClass.STANDARD:
            return await self._hold(classification, session_id, window_title, element_name, mode)

        # 9. Ordinary input under a live grant — the ONE outcome that moves the
        #    operator's mouse, so it is also the one that must leave a trace.
        #    Everything else here logs its refusal; an allow that logged nothing
        #    would make the acted-upon case the only invisible one.
        expires_at = (now + timedelta(seconds=action_ttl_seconds())).isoformat()
        logger.info(
            "Desktop gate ALLOWED %s:%s:%s on %r in %r (grant=%s, expires=%s)",
            domain,
            verb,
            risk,
            element_name,
            window_title,
            grant.get("id"),
            expires_at,
        )
        await self._emit_allowed(window_title, element_name, grant.get("id"))
        return DesktopGateDecision(
            allow=True,
            reason="session_grant",
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
        self, session_id: str, window_title: str, now: datetime
    ) -> tuple[dict | None, str]:
        """The owner's live grant for this session AND window, or ``(None, reason)``.

        Six independent bars, each of which has to hold. Every one exists
        because its absence was a defect found in review, not because it seemed
        prudent:

        - **this session** — the grant carries the session id and is filtered on
          it in SQL, so one session's consent is never another's;
        - **a GRANT, not a hold** — both are rows of the same action_type, so
          ``kind`` is what separates "consented to this session" from "approved
          one click". Without it, approving a single held action silently became
          a full session grant with a fresh expiry;
        - **this WINDOW** — the grant names the target window the owner was
          shown, and the action must be in it. Carrying that name without
          comparing it is worse than not carrying it: the consent card reads the
          window back to the operator, so an uncompared field is a promise the
          card makes and the code does not keep. MEASURED before this bar
          existed — a grant for one window authorised actions in any other;
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
        target = _window_key(window_title)
        saw_allowlisted = False
        saw_this_window = False

        for row in rows:
            resolved_by = str(row.get("resolved_by") or "")
            if not resolved_by.startswith(DESKTOP_GRANT_RESOLVER_PREFIXES):
                continue
            saw_allowlisted = True

            granted = _window_key(_grant_window(row))
            # An absent or empty window on EITHER side is a refusal, never a
            # wildcard — the rule the device already applies to a target it
            # cannot resolve. A grant naming no window is not a narrower grant;
            # it is an unbounded one.
            if not granted or not target or granted != target:
                continue
            saw_this_window = True

            resolved_at = _parse_ts(row.get("resolved_at"))
            if resolved_at is None:
                # An approved row with an unreadable resolution time cannot be
                # aged, and an un-ageable grant is an unbounded one. Refuse.
                logger.warning(
                    "Desktop grant %s has unparseable resolved_at %r — refusing",
                    row.get("id"),
                    row.get("resolved_at"),
                )
                continue

            age = now - resolved_at
            # Bounded on BOTH sides. A future-dated resolution gives a negative
            # age, which `age <= ttl` alone accepts forever — a backwards clock
            # step or a hand-edited row would mint a permanent grant.
            if timedelta(0) <= age <= ttl:
                return row, "session_grant"

        if not saw_allowlisted:
            return None, "grant_not_human"
        if not saw_this_window:
            return None, "grant_window_mismatch"
        return None, "grant_expired"

    async def _hold(
        self,
        classification: DesktopActionClassification,
        session_id: str,
        window_title: str,
        element_name: str,
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
                "kind": DESKTOP_GATE_ACTION_TYPE,
                "cell": [domain, verb, risk],
                "session_id": session_id,
                "window_title": window_title,
                "element_name": element_name,
                "sub_class": classification.sub_class,
            }
        )
        request_id = await self._approval.request_approval(
            action_type=DESKTOP_GATE_ACTION_TYPE,
            action_class=str(classification.action_class),
            description=(
                f"Desktop {classification.sub_class} action on "
                f"'{_display(element_name) or 'an unnamed control'}' in "
                f"'{_display(window_title)}' — approving lets the session "
                "re-plan from a fresh capture; it does not replay this action"
            ),
            context=context,
            # Wait for the owner — never auto-approve, never auto-drop. This
            # holds only while DESKTOP_GATE_ACTION_TYPE is absent from the
            # timeout tables (a null timeout is what makes expire_timed_out
            # skip the row); test_desktop_gate pins that.
            timeout_seconds=None,
        )
        await self._emit_held(classification, window_title, element_name)
        logger.info(
            "Desktop gate HELD %s action (cell=%s:%s:%s, window=%r, request=%s)",
            classification.sub_class,
            domain,
            verb,
            risk,
            window_title,
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


def _grant_window(row: dict) -> str:
    """The window title a grant row was issued for, or "" if unreadable.

    The SQL predicate already guards `json_valid`, but a row can be valid JSON
    and still lack the key (a hand-written row, an older wire format), so an
    unreadable window resolves to "" — which the caller treats as a refusal
    rather than a wildcard.
    """
    try:
        ctx = json.loads(row.get("context") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(ctx.get("window_title") or "") if isinstance(ctx, dict) else ""


def _window_key(value: object) -> str:
    """Comparison key for a window title.

    Case- and whitespace-insensitive, and nothing more. Window titles are not
    stable identifiers — a document name changes the title of the same window —
    so this is a STRICT bar by choice: a title that drifts refuses, and the loop
    re-asks against the window the operator can actually see named. The safe
    direction for a boundary the classifier falls back on is to refuse and
    re-ask, not to guess that two different titles mean the same window.
    A stable window identity (a handle carried from the resolve step) is the
    right long-term fix and belongs with the loop that resolves it.
    """
    return " ".join(str(value or "").split()).casefold()


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
