"""Sentinel dispatcher — the container-side guardian's CC session orchestrator.

The Sentinel is Genesis's first real autonomous CC call site. It diagnoses
infrastructure problems and FIXES them. It is reactive — activated by fire
alarms, Guardian dialogue, remediation exhaustion, or the infrastructure
monitor. It is NOT a polling loop.

The dispatcher manages the CC session lifecycle: per-pattern exponential
backoff, concurrency, state machine transitions, approval gates, and shared
filesystem writes. The actual diagnosis and fix happen inside the CC session
itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from genesis.sentinel.classifier import FireAlarm, classify_alerts, worst_tier
from genesis.sentinel.context import assemble_diagnostic_context
from genesis.sentinel.shared import append_log, write_last_run, write_state_for_guardian
from genesis.sentinel.state import (
    SentinelState,
    SentinelStateData,
    load_state,
    save_state,
)

if TYPE_CHECKING:
    from genesis.autonomy.remediation import RemediationRegistry
    from genesis.cc.invoker import CCInvoker
    from genesis.cc.session_manager import SessionManager
    from genesis.observability.events import GenesisEventBus
    from genesis.observability.health_data import HealthDataService

logger = logging.getLogger(__name__)

# Per-pattern exponential backoff. Index = prior attempt count, value = seconds
# to wait since the last attempt before trying again. Attempt 0 is immediate.
# After the last entry is consumed, the pattern escalates to the user instead
# of auto-dispatching. Tuned so that:
#   attempt 1: now
#   attempt 2: 15 min after attempt 1
#   attempt 3: 45 min after attempt 2
#   attempt 4: 2 h   after attempt 3
#   attempt 5: escalate
#
# The user wanted this, verbatim: "exponential backoff and escalate at
# threshold. It needs to be Genesis escalating to me."
_BACKOFF_SCHEDULE_S: tuple[float, ...] = (0.0, 15 * 60, 45 * 60, 2 * 60 * 60)
_ESCALATE_AT_ATTEMPT = len(_BACKOFF_SCHEDULE_S) + 1  # 5

# Ring buffer size for 2-of-N debouncing. Jay was explicit: "It doesn't need
# to be two consecutive ticks — it's just two out of three. Consecutive or not."
_ALARM_RING_SIZE = 3
_ALARM_CONFIRMATION_COUNT = 2


def _serialize_request(request: SentinelRequest) -> str:
    """Serialize a SentinelRequest to JSON for state persistence."""
    import json
    return json.dumps({
        "trigger_source": request.trigger_source,
        "trigger_reason": request.trigger_reason,
        "tier": request.tier,
        "alarms": [asdict(a) for a in request.alarms],
        "context": request.context,
    })


def _deserialize_request(raw: str) -> SentinelRequest | None:
    """Deserialize a SentinelRequest from JSON. Returns None on failure."""
    if not raw:
        return None
    try:
        import json
        data = json.loads(raw)
        alarms = [FireAlarm(**a) for a in data.get("alarms", [])]
        return SentinelRequest(
            trigger_source=data["trigger_source"],
            trigger_reason=data["trigger_reason"],
            tier=data.get("tier"),
            alarms=alarms,
            context=data.get("context", {}),
        )
    except Exception:
        logger.error("Failed to deserialize SentinelRequest", exc_info=True)
        return None


def _serialize_result(result: SentinelResult) -> str:
    """Serialize a SentinelResult to JSON for state persistence."""
    import json
    return json.dumps({
        "dispatched": result.dispatched,
        "session_id": result.session_id,
        "diagnosis": result.diagnosis,
        "actions_taken": result.actions_taken,
        "proposed_actions": result.proposed_actions,
        "resolved": result.resolved,
        "observation_id": result.observation_id,
        "reason": result.reason,
        "duration_s": result.duration_s,
    })


def _deserialize_result(raw: str) -> SentinelResult | None:
    """Deserialize a SentinelResult from JSON. Returns None on failure."""
    if not raw:
        return None
    try:
        import json
        data = json.loads(raw)
        return SentinelResult(**{
            k: v for k, v in data.items()
            if k in SentinelResult.__dataclass_fields__
        })
    except Exception:
        logger.error("Failed to deserialize SentinelResult", exc_info=True)
        return None


@dataclass(frozen=True)
class SentinelRequest:
    """Request to wake the Sentinel."""

    trigger_source: str  # "fire_alarm", "guardian_dialogue", "remediation_exhausted", etc.
    trigger_reason: str
    tier: int | None = None  # 1, 2, or 3 (None if not classified)
    alarms: list[FireAlarm] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class SentinelResult:
    """Result of a Sentinel dispatch."""

    dispatched: bool
    session_id: str = ""
    diagnosis: str = ""
    actions_taken: list[str] = field(default_factory=list)
    proposed_actions: list[dict] = field(default_factory=list)
    resolved: bool = False
    observation_id: str = ""
    reason: str = ""  # Why dispatched or skipped
    duration_s: float = 0.0


def _extract_pattern(request: SentinelRequest) -> str:
    """Derive a stable backoff-key string from a dispatch request.

    Fire-alarm triggers use the worst alarm's alert_id directly (e.g.
    "memory:critical", "infra:disk_low"). Direct escalations from other
    subsystems don't have an alarm — fall back to the trigger_source so
    each subsystem still gets its own backoff bucket.
    """
    if request.alarms:
        # Worst alarm was sorted first by the classifier
        return request.alarms[0].alert_id
    return f"direct:{request.trigger_source}"


class SentinelDispatcher:
    """Orchestrates Sentinel CC sessions for infrastructure incident response.

    The Sentinel is the container-side counterpart to the external Guardian.
    It is reactive — triggered by fire alarms, Guardian dialogue, or
    remediation exhaustion. It dispatches a CC background session that
    diagnoses the problem, requests approval via Telegram, and fixes it.
    """

    def __init__(
        self,
        *,
        session_manager: SessionManager | None = None,
        invoker: CCInvoker | None = None,
        remediation_registry: RemediationRegistry | None = None,
        db=None,
        event_bus: GenesisEventBus | None = None,
        health_data: HealthDataService | None = None,
        outreach_pipeline=None,
        approval_gate=None,
    ) -> None:
        self._session_manager = session_manager
        self._invoker = invoker
        self._remediation_registry = remediation_registry
        self._db = db
        self._event_bus = event_bus
        self._health_data = health_data
        self._outreach_pipeline = outreach_pipeline
        self._approval_gate = approval_gate  # AutonomousCliApprovalGate or None
        self._lock = asyncio.Lock()
        self._active_session_id: str | None = None
        self._state = load_state()

        # Per-pattern exponential backoff (in-memory; resets on restart).
        # key = pattern string (usually alert_id), value = list of monotonic
        # timestamps of prior dispatch attempts for that pattern.
        self._pattern_attempts: dict[str, list[float]] = {}

        # Patterns that hit the escalation threshold and are now held back
        # until the user intervenes. Value = iso timestamp of escalation.
        # Cleared on process restart or resolved dispatch of the same pattern.
        self._escalated_patterns: dict[str, str] = {}

        # Ring buffer of alarm id sets seen on recent ticks. Drives 2-of-N
        # debouncing — a pattern must appear in ≥_ALARM_CONFIRMATION_COUNT
        # of the last _ALARM_RING_SIZE ticks before we even consider
        # dispatching. Prevents single-tick flaps from waking the Sentinel.
        self._recent_alarm_sets: deque[set[str]] = deque(maxlen=_ALARM_RING_SIZE)

        # Load dispatch approval policy (legacy path, used when approval_gate is None)
        try:
            from genesis.autonomy.cli_policy import load_autonomous_cli_policy
            policy = load_autonomous_cli_policy()
            self._require_approval = policy.as_dict().get(
                "manual_approval_required_sentinel", True,
            )
        except Exception:
            self._require_approval = True

        # On startup, clean up stale AWAITING_* states if no matching
        # approval row exists in the DB. This handles restarts where the
        # approval was resolved while the process was down.
        if self._state.state in (
            SentinelState.AWAITING_DISPATCH_APPROVAL,
            SentinelState.AWAITING_ACTION_APPROVAL,
        ) and not self._state.pending_request_id:
            logger.info(
                "Clearing stale %s state (no pending request id)",
                self._state.current_state,
            )
            self._state.clear_pending()
            self._state.transition(SentinelState.HEALTHY, reason="stale pending cleared on startup")
            save_state(self._state)

    async def dispatch(self, request: SentinelRequest) -> SentinelResult:
        """Main entry point. Evaluate request, manage state, dispatch CC if needed.

        Gate checks (in order):
        1. Bootstrap grace period
        2. Already running (max 1 concurrent)
        3. Per-pattern exponential backoff (replaces global cooldown + daily budget)
        4. CC infrastructure available
        """
        # Auto-reset ESCALATED if timeout expired
        if self._state.should_auto_reset_escalated():
            self._state.escalated_count += 1
            self._state.transition(
                SentinelState.HEALTHY,
                reason=f"auto-reset from ESCALATED (count={self._state.escalated_count})",
            )
            save_state(self._state)

        async with self._lock:
            return await self._gated_dispatch(request)

    async def _gated_dispatch(self, request: SentinelRequest) -> SentinelResult:
        """Gate checks and dispatch, protected by asyncio.Lock."""
        # Gate 1: Bootstrap grace
        if self._state.in_bootstrap_grace():
            return SentinelResult(
                dispatched=False,
                reason="In bootstrap grace period — skipping",
            )

        # Gate 2: Awaiting approval (non-blocking gate)
        if self._state.state in (
            SentinelState.AWAITING_DISPATCH_APPROVAL,
            SentinelState.AWAITING_ACTION_APPROVAL,
        ):
            return SentinelResult(
                dispatched=False,
                reason=f"Sentinel awaiting approval ({self._state.pending_policy_id})",
            )

        # Gate 3: Concurrent limit
        if self._active_session_id is not None:
            return SentinelResult(
                dispatched=False,
                reason=f"Sentinel session already active: {self._active_session_id}",
            )

        # Gate 3: Per-pattern backoff (replaces both global cooldown and
        # daily budget). If the pattern has burned through its attempts,
        # escalate to the user and suppress further auto-dispatch.
        pattern = _extract_pattern(request)
        ready, reason = self._backoff_ready(pattern)
        if not ready:
            return SentinelResult(dispatched=False, reason=reason)

        # Gate 4: CC infrastructure
        if self._invoker is None or self._session_manager is None:
            return SentinelResult(
                dispatched=False,
                reason="CC invoker or session manager not available",
            )

        # Passed all gates — check if this attempt crosses the escalation
        # threshold. If so, post an escalation message instead of dispatching
        # CC, and hold the pattern until the user intervenes (or restart).
        attempt_number = len(self._pattern_attempts.get(pattern, [])) + 1
        if attempt_number >= _ESCALATE_AT_ATTEMPT:
            return await self._escalate_pattern(request, pattern, attempt_number)

        # Record the attempt BEFORE dispatch so concurrent callers see it.
        self._pattern_attempts.setdefault(pattern, []).append(time.monotonic())

        return await self._execute_dispatch(request, pattern=pattern)

    async def _execute_dispatch(
        self, request: SentinelRequest, *, pattern: str = "",
    ) -> SentinelResult:
        """Execute the actual CC dispatch with state management.

        Three-phase flow when approval_gate is present:
          Phase 1: ensure_approval(sentinel_dispatch) → park if pending
          Phase 2: CC session → get diagnosis + proposed actions
          Phase 3: ensure_approval(sentinel_action) → park if pending, then execute
        Legacy path (approval_gate is None): blocking Telegram approval.
        """
        start = time.monotonic()

        # ── Phase 1: Dispatch approval ──────────────────────────────
        if self._approval_gate is not None:
            try:
                status, request_id, reason = await self._approval_gate.ensure_approval(
                    subsystem="sentinel",
                    policy_id="sentinel_dispatch",
                    action_label=f"Sentinel dispatch: {request.trigger_reason[:60]}",
                    action_type="sentinel_dispatch",
                    extra_context={
                        "tier_label": f"Tier {request.tier}" if request.tier else "Unknown tier",
                        "trigger_source": request.trigger_source,
                        "trigger_reason": request.trigger_reason,
                        "alarm_count": len(request.alarms),
                    },
                )
                if status == "pending":
                    # Park: save context and wait for awareness loop resume
                    self._state.pending_request_id = request_id or ""
                    self._state.pending_policy_id = "sentinel_dispatch"
                    self._state.pending_pattern = pattern
                    self._state.pending_request_json = _serialize_request(request)
                    self._state.transition(
                        SentinelState.AWAITING_DISPATCH_APPROVAL,
                        reason=f"dispatch approval pending ({request_id})",
                    )
                    save_state(self._state)
                    append_log({"event": "dispatch_approval_pending", "request_id": request_id})
                    return SentinelResult(
                        dispatched=False,
                        reason=f"Dispatch approval pending: {reason}",
                        duration_s=time.monotonic() - start,
                    )
                if status == "rejected":
                    expiry = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
                    self._state.rejected_patterns[pattern] = expiry
                    self._state.clear_pending()
                    self._state.transition(SentinelState.HEALTHY, reason="dispatch rejected via approval gate")
                    save_state(self._state)
                    append_log({"event": "dispatch_rejected", "request_id": request_id})
                    return SentinelResult(
                        dispatched=False,
                        reason="User rejected Sentinel dispatch",
                        duration_s=time.monotonic() - start,
                    )
                # status == "approved" — continue to Phase 2
                logger.info("Sentinel dispatch approved (%s)", request_id)
                append_log({"event": "dispatch_approved", "request_id": request_id})
            except Exception:
                logger.error("Sentinel dispatch approval failed", exc_info=True)
                return SentinelResult(
                    dispatched=False,
                    reason="Dispatch approval mechanism failed",
                    duration_s=time.monotonic() - start,
                )
        elif self._require_approval and self._outreach_pipeline:
            # Legacy blocking path
            try:
                approved = await self._request_dispatch_approval(request)
                if not approved:
                    expiry = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
                    self._state.rejected_patterns[pattern] = expiry
                    self._state.transition(SentinelState.HEALTHY, reason="dispatch rejected by user")
                    save_state(self._state)
                    return SentinelResult(
                        dispatched=False,
                        reason="User rejected Sentinel dispatch via Telegram",
                        duration_s=time.monotonic() - start,
                    )
            except Exception:
                logger.error("Sentinel dispatch approval failed", exc_info=True)
                return SentinelResult(
                    dispatched=False,
                    reason="Dispatch approval mechanism failed",
                    duration_s=time.monotonic() - start,
                )

        # ── Phase 2: CC session ─────────────────────────────────────
        return await self._phase2_cc_and_actions(request, pattern=pattern, start=start)

    async def _phase2_cc_and_actions(
        self, request: SentinelRequest, *, pattern: str = "", start: float = 0.0,
    ) -> SentinelResult:
        """Phase 2-3: Run CC session, then request action approval and execute.

        Split from _execute_dispatch so the awareness loop can resume here
        after a dispatch approval is granted.
        """
        if start == 0.0:
            start = time.monotonic()

        # Transition to INVESTIGATING
        self._state.transition(
            SentinelState.INVESTIGATING,
            reason=f"{request.trigger_source}: {request.trigger_reason}",
        )
        self._state.last_trigger_source = request.trigger_source
        self._state.clear_pending()
        save_state(self._state)

        # Log the dispatch
        append_log({
            "event": "dispatch_started",
            "trigger_source": request.trigger_source,
            "trigger_reason": request.trigger_reason,
            "tier": request.tier,
            "alarm_count": len(request.alarms),
        })

        # Emit event
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.INFO,
                "sentinel.dispatched",
                f"Sentinel dispatched: {request.trigger_source} — {request.trigger_reason}",
            )

        # Transition to REMEDIATING and dispatch CC
        self._state.transition(SentinelState.REMEDIATING, reason="dispatching CC session")
        save_state(self._state)

        try:
            result = await self._dispatch_cc_session(request)
        except Exception as exc:
            logger.error("Sentinel CC dispatch failed", exc_info=True)
            result = SentinelResult(
                dispatched=False,
                reason=f"CC dispatch error: {exc}",
            )

        # ── Phase 3: Action approval + execution ───────────────────
        if result.dispatched and result.proposed_actions:
            if self._approval_gate is not None:
                try:
                    action_result = await self._gated_action_approval(result, request, pattern)
                    if action_result is not None:
                        # Pending or rejected — return the phase-3 result
                        action_result.duration_s = time.monotonic() - start
                        return action_result
                    # None means approved — continue to execution below
                except Exception:
                    logger.error("Sentinel action approval/execution failed", exc_info=True)
            elif self._outreach_pipeline:
                # Legacy blocking path
                try:
                    executed = await self._approve_and_execute_actions(result)
                    if executed:
                        result.resolved = True
                        result.actions_taken = [a["command"] for a in executed]
                except Exception:
                    logger.error("Sentinel action approval/execution failed", exc_info=True)

            # Execute approved actions (gated path)
            if self._approval_gate is not None and result.proposed_actions and not result.resolved:
                try:
                    executed = await self._execute_approved_actions(result.proposed_actions)
                    if executed:
                        result.resolved = True
                        result.actions_taken = [a["command"] for a in executed]
                except Exception:
                    logger.error("Sentinel action execution failed", exc_info=True)

        return await self._finalize_dispatch(request, result, pattern=pattern, start=start)

    async def _gated_action_approval(
        self, result: SentinelResult, request: SentinelRequest, pattern: str,
    ) -> SentinelResult | None:
        """Phase 3 approval gate for proposed actions.

        Returns SentinelResult if pending/rejected (caller should return it).
        Returns None if approved (caller should proceed to execute actions).
        """
        status, request_id, reason = await self._approval_gate.ensure_approval(
            subsystem="sentinel",
            policy_id="sentinel_action",
            action_label=f"Sentinel actions: {result.diagnosis[:40]}",
            action_type="sentinel_action",
            extra_context={
                "diagnosis": result.diagnosis,
                "proposed_actions": result.proposed_actions,
                "session_id": result.session_id,
            },
        )

        if status == "pending":
            # Park: save CC result for resume
            self._state.pending_request_id = request_id or ""
            self._state.pending_policy_id = "sentinel_action"
            self._state.pending_pattern = pattern
            self._state.pending_request_json = _serialize_request(request)
            self._state.pending_cc_result_json = _serialize_result(result)
            self._state.transition(
                SentinelState.AWAITING_ACTION_APPROVAL,
                reason=f"action approval pending ({request_id})",
            )
            save_state(self._state)
            append_log({"event": "action_approval_pending", "request_id": request_id})
            return SentinelResult(
                dispatched=True,
                session_id=result.session_id,
                diagnosis=result.diagnosis,
                proposed_actions=result.proposed_actions,
                reason=f"Action approval pending: {reason}",
            )

        if status == "rejected":
            self._state.clear_pending()
            append_log({"event": "actions_rejected", "request_id": request_id})
            return SentinelResult(
                dispatched=True,
                session_id=result.session_id,
                diagnosis=result.diagnosis,
                reason="User rejected Sentinel actions",
            )

        # Approved — return None so caller executes actions
        logger.info("Sentinel actions approved (%s)", request_id)
        append_log({"event": "actions_approved", "request_id": request_id})
        return None

    async def _execute_approved_actions(self, proposed_actions: list[dict]) -> list[dict]:
        """Execute a list of approved shell commands."""
        executed = []
        for action in proposed_actions:
            cmd = action.get("command", "")
            if not cmd:
                continue
            try:
                logger.info("Sentinel executing: %s", cmd)
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=60.0,
                )
                success = proc.returncode == 0
                action["success"] = success
                action["stdout"] = stdout.decode("utf-8", errors="replace")[:500]
                action["stderr"] = stderr.decode("utf-8", errors="replace")[:500]
                executed.append(action)
                if success:
                    logger.info("Sentinel action succeeded: %s", cmd)
                else:
                    logger.error(
                        "Sentinel action failed (rc=%d): %s — %s",
                        proc.returncode, cmd, stderr.decode("utf-8", errors="replace")[:200],
                    )
            except TimeoutError:
                logger.error("Sentinel action timed out: %s", cmd)
                action["success"] = False
                action["stderr"] = "Timed out after 60s"
                executed.append(action)
            except OSError as exc:
                logger.error("Sentinel action OS error: %s — %s", cmd, exc)
        return executed

    async def _finalize_dispatch(
        self, request: SentinelRequest, result: SentinelResult,
        *, pattern: str = "", start: float = 0.0,
    ) -> SentinelResult:
        """Common finalization: state transitions, logging, observation creation."""
        duration = time.monotonic() - start
        result.duration_s = duration

        # Update state based on result
        if result.dispatched and result.resolved:
            self._state.transition(SentinelState.HEALTHY, reason="resolved after action execution")
            self._state.escalated_count = 0  # Reset oscillation counter
            # Clear backoff attempts for this pattern — the problem is fixed.
            # Next occurrence starts from attempt 1.
            if pattern and pattern in self._pattern_attempts:
                del self._pattern_attempts[pattern]
            if pattern and pattern in self._escalated_patterns:
                del self._escalated_patterns[pattern]
            if pattern and pattern in self._state.rejected_patterns:
                del self._state.rejected_patterns[pattern]
        elif result.dispatched:
            self._state.transition(SentinelState.ESCALATED, reason=result.reason or "CC could not resolve")
        else:
            self._state.transition(SentinelState.ESCALATED, reason=result.reason or "dispatch failed")

        self._state.clear_pending()
        self._state.record_cc_dispatch()
        save_state(self._state)
        write_state_for_guardian(asdict(self._state))

        # Write to shared filesystem for Guardian
        write_last_run(
            trigger_source=request.trigger_source,
            tier=request.tier,
            diagnosis=result.diagnosis,
            actions_taken=result.actions_taken,
            resolved=result.resolved,
            duration_s=duration,
            session_id=result.session_id,
        )

        append_log({
            "event": "dispatch_completed",
            "dispatched": result.dispatched,
            "resolved": result.resolved,
            "diagnosis": result.diagnosis[:200] if result.diagnosis else "",
            "actions_taken": result.actions_taken,
            "duration_s": round(duration, 1),
            "session_id": result.session_id,
        })

        # Create observation for ego
        if self._db is not None:
            try:
                await self._create_observation(request, result)
            except Exception:
                logger.error("Failed to create sentinel observation", exc_info=True)

        # Emit completion event
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            severity = Severity.INFO if result.resolved else Severity.WARNING
            await self._event_bus.emit(
                Subsystem.GUARDIAN, severity,
                "sentinel.completed",
                f"Sentinel {'resolved' if result.resolved else 'escalated'}: "
                f"{result.diagnosis[:100] if result.diagnosis else 'no diagnosis'}",
            )

        return result

    async def resume_from_approval(self, request_id: str, status: str) -> SentinelResult | None:
        """Resume a parked dispatch after the user grants approval.

        Called by the awareness loop when it finds an approved sentinel
        approval row. Returns None if the state doesn't match (stale resume).
        """
        if status == "rejected":
            return await self.handle_approval_resolution(request_id, "rejected")

        policy_id = self._state.pending_policy_id
        if not policy_id or self._state.pending_request_id != request_id:
            logger.warning(
                "Sentinel resume: request_id %s doesn't match pending %s — skipping",
                request_id, self._state.pending_request_id,
            )
            return None

        pattern = self._state.pending_pattern
        request = _deserialize_request(self._state.pending_request_json)
        if request is None:
            logger.error("Sentinel resume: failed to deserialize pending request")
            self._state.clear_pending()
            self._state.transition(SentinelState.HEALTHY, reason="resume failed: bad pending data")
            save_state(self._state)
            return None

        async with self._lock:
            if policy_id == "sentinel_dispatch":
                logger.info("Resuming sentinel dispatch after approval %s", request_id)
                return await self._phase2_cc_and_actions(request, pattern=pattern)
            if policy_id == "sentinel_action":
                logger.info("Resuming sentinel actions after approval %s", request_id)
                cc_result = _deserialize_result(self._state.pending_cc_result_json)
                if cc_result is None:
                    logger.error("Sentinel resume: failed to deserialize CC result")
                    self._state.clear_pending()
                    self._state.transition(SentinelState.HEALTHY, reason="resume failed: bad CC result")
                    save_state(self._state)
                    return None
                # Execute the approved actions
                try:
                    executed = await self._execute_approved_actions(cc_result.proposed_actions)
                    if executed:
                        cc_result.resolved = True
                        cc_result.actions_taken = [a["command"] for a in executed]
                except Exception:
                    logger.error("Sentinel action execution failed on resume", exc_info=True)
                return await self._finalize_dispatch(request, cc_result, pattern=pattern)

        return None

    async def handle_approval_resolution(
        self, request_id: str, status: str,
    ) -> SentinelResult | None:
        """Handle a rejected or cancelled approval.

        Called by the awareness loop or alarm-clearing cancellation.
        """
        if self._state.pending_request_id != request_id:
            return None

        pattern = self._state.pending_pattern
        policy_id = self._state.pending_policy_id

        if status == "rejected":
            if pattern:
                expiry = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
                self._state.rejected_patterns[pattern] = expiry
            self._state.clear_pending()
            self._state.transition(SentinelState.HEALTHY, reason=f"{policy_id} rejected by user")
            save_state(self._state)
            append_log({"event": f"{policy_id}_rejected", "request_id": request_id})
            return SentinelResult(dispatched=False, reason=f"{policy_id} rejected")

        if status == "cancelled":
            self._state.clear_pending()
            self._state.transition(SentinelState.HEALTHY, reason=f"{policy_id} cancelled (alarm cleared)")
            save_state(self._state)
            append_log({"event": f"{policy_id}_cancelled", "request_id": request_id})
            return SentinelResult(dispatched=False, reason=f"{policy_id} cancelled: alarm cleared")

        return None

    async def _request_dispatch_approval(self, request: SentinelRequest) -> bool:
        """Send Telegram approval request with inline buttons and wait.

        Returns True if approved, False if rejected or timed out.
        Primary UX: Approve/Reject inline keyboard buttons.
        Fallback: quote-reply with approve/reject text.
        """
        import uuid as _uuid

        from genesis.autonomy.autonomous_dispatch import _reply_decision
        from genesis.outreach.pipeline import OutreachCategory, OutreachRequest

        tier_label = f"Tier {request.tier}" if request.tier else "Unknown tier"
        message = (
            f"🛡️ <b>Sentinel Activation Request</b>\n\n"
            f"The Sentinel detected a <b>{tier_label}</b> fire alarm and wants to "
            f"investigate and fix the issue.\n\n"
            f"<b>Trigger:</b> {request.trigger_source}\n"
            f"<b>Reason:</b> {request.trigger_reason}"
        )

        # Build inline keyboard buttons
        waiter_key = str(_uuid.uuid4())
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data=f"approve:{waiter_key}"),
                InlineKeyboardButton("Reject", callback_data=f"reject:{waiter_key}"),
            ]])
        except ImportError:
            keyboard = None
            waiter_key = None  # Fall back to text-only
            message += (
                "\n\nReply <b>approve</b> (or yes/ok/go) to activate, "
                "or <b>reject</b> (or no) to cancel."
            )

        outreach_result, reply = await self._outreach_pipeline.submit_raw_and_wait(
            message,
            OutreachRequest(
                category=OutreachCategory.BLOCKER,
                topic=f"Sentinel: {request.trigger_reason[:60]}",
                context=message,
                salience_score=1.0,
                signal_type="sentinel_approval",
                source_id=f"sentinel-dispatch:{request.trigger_source}:{int(time.time())}",
            ),
            timeout_s=300.0,
            reply_markup=keyboard,
            waiter_key=waiter_key,
        )

        if not reply:
            logger.warning("Sentinel dispatch approval timed out (no response in 300s)")
            append_log({"event": "dispatch_approval_timeout", "trigger": request.trigger_source})
            return False

        decision = _reply_decision(reply)
        if decision == "approved":
            logger.info("Sentinel dispatch approved by user: %r", reply)
            append_log({"event": "dispatch_approved", "reply": reply})
            return True
        if decision == "rejected":
            logger.info("Sentinel dispatch rejected by user: %r", reply)
            append_log({"event": "dispatch_rejected", "reply": reply})
            return False

        # Ambiguous reply — treat as rejection for safety
        logger.warning("Sentinel dispatch: ambiguous reply %r — treating as rejection", reply)
        append_log({"event": "dispatch_ambiguous", "reply": reply})
        return False

    async def _dispatch_cc_session(self, request: SentinelRequest) -> SentinelResult:
        """Dispatch the actual CC background session."""
        from genesis.cc.types import CCInvocation, CCModel, EffortLevel, SessionType

        # Assemble diagnostic context
        health_snapshot = None
        if self._health_data is not None:
            try:
                health_snapshot = await self._health_data.snapshot()
            except Exception:
                logger.warning("Failed to get health snapshot for Sentinel", exc_info=True)

        context_str = await assemble_diagnostic_context(
            alarms=request.alarms,
            trigger_source=request.trigger_source,
            trigger_reason=request.trigger_reason,
            health_snapshot=health_snapshot,
            db=self._db,
        )

        # Load the Sentinel prompt
        prompt_path = __import__("pathlib").Path(__file__).parent / "prompts" / "SENTINEL.md"
        try:
            system_prompt = prompt_path.read_text()
        except FileNotFoundError:
            system_prompt = (
                "You are the Sentinel — Genesis's internal health guardian. "
                "Diagnose the problem and fix it. Use outreach_send_and_wait "
                "to get approval before taking any action."
            )

        full_prompt = f"{context_str}\n\n---\n\nDiagnose and fix the above issues."

        # Create background session
        session = await self._session_manager.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=CCModel.SONNET,
            effort=EffortLevel.HIGH,
            source_tag="sentinel",
        )
        session_id = session.get("id", "")
        self._active_session_id = session_id

        try:
            # Build MCP config so the CC session has health + outreach tools
            mcp_path = None
            try:
                from genesis.cc.session_config import SessionConfigBuilder
                mcp_path = SessionConfigBuilder().build_mcp_config("sentinel")
            except Exception:
                logger.warning("Failed to build MCP config for Sentinel", exc_info=True)

            invocation = CCInvocation(
                prompt=full_prompt,
                model=CCModel.SONNET,
                effort=EffortLevel.HIGH,
                timeout_s=900,
                skip_permissions=True,
                system_prompt=system_prompt,
                append_system_prompt=True,
                output_format="text",
                mcp_config=mcp_path,
            )

            output = await self._invoker.run(invocation)

            # Parse output
            result = self._parse_output(output, session_id)
            return result
        finally:
            self._active_session_id = None
            # End session
            try:
                await self._session_manager.complete(session_id)
            except Exception:
                logger.debug("Failed to end sentinel session", exc_info=True)

    def _parse_output(self, output, session_id: str) -> SentinelResult:
        """Parse the CC session output into a SentinelResult."""
        import json
        import re

        text = ""
        if hasattr(output, "text"):
            text = output.text or ""
        elif hasattr(output, "result"):
            text = output.result or ""
        elif isinstance(output, str):
            text = output

        parsed: dict = {}

        def _try_parse(candidate: str) -> bool:
            nonlocal parsed
            try:
                data = json.loads(candidate)
                if isinstance(data, dict) and "diagnosis" in data:
                    parsed = data
                    return True
            except (json.JSONDecodeError, ValueError):
                pass
            return False

        # Strategy 1: Extract JSON from markdown code blocks (```json ... ```)
        for block in re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
            if _try_parse(block.strip()):
                break

        # Strategy 2: Find standalone JSON object in text
        if not parsed:
            for match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text):
                if _try_parse(match.group()):
                    break

        # Strategy 3: Try the full text as JSON
        if not parsed:
            _try_parse(text.strip())

        diagnosis = parsed.get("diagnosis", "")
        proposed_actions = parsed.get("proposed_actions", [])
        actions_taken = parsed.get("actions_taken", [])
        resolved = parsed.get("resolved", False)

        # Fallback: use the full text as diagnosis
        if not diagnosis and text:
            diagnosis = text[:500]

        return SentinelResult(
            dispatched=True,
            session_id=session_id,
            diagnosis=diagnosis,
            actions_taken=actions_taken,
            proposed_actions=proposed_actions,
            resolved=resolved,
            reason="actions proposed" if proposed_actions else ("resolved" if resolved else "no actions proposed — escalating"),
        )

    async def _approve_and_execute_actions(self, result: SentinelResult) -> list[dict]:
        """Send proposed actions to user via Telegram with inline buttons.

        Returns the list of actions that were executed, or empty list.
        """
        import asyncio
        import uuid as _uuid

        if not self._outreach_pipeline or not result.proposed_actions:
            return []

        from genesis.autonomy.autonomous_dispatch import _reply_decision
        from genesis.outreach.pipeline import OutreachCategory, OutreachRequest

        # Format the actions for the user
        action_lines = []
        for i, action in enumerate(result.proposed_actions, 1):
            desc = action.get("description", "Unknown action")
            cmd = action.get("command", "")
            safe = "safe" if action.get("safe") else "potentially unsafe"
            action_lines.append(f"{i}. {desc}\n   <code>{cmd}</code> ({safe})")

        message = (
            f"🛡️ <b>Sentinel Action Approval</b>\n\n"
            f"<b>Diagnosis:</b> {result.diagnosis[:200]}\n\n"
            f"<b>Proposed actions:</b>\n" +
            "\n".join(action_lines)
        )

        # Build inline keyboard buttons
        waiter_key = str(_uuid.uuid4())
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Approve", callback_data=f"approve:{waiter_key}"),
                InlineKeyboardButton("Reject", callback_data=f"reject:{waiter_key}"),
            ]])
        except ImportError:
            keyboard = None
            waiter_key = None
            message += "\n\nReply <b>approve</b> to execute all, or <b>reject</b> to cancel."

        outreach_result, reply = await self._outreach_pipeline.submit_raw_and_wait(
            message,
            OutreachRequest(
                category=OutreachCategory.BLOCKER,
                topic=f"Sentinel actions: {result.diagnosis[:40]}",
                context=message,
                salience_score=1.0,
                signal_type="sentinel_action_approval",
                source_id=f"sentinel-action:{int(time.time())}",
            ),
            timeout_s=300.0,
            reply_markup=keyboard,
            waiter_key=waiter_key,
        )

        if not reply:
            logger.warning("Sentinel action approval timed out")
            append_log({"event": "action_approval_timeout", "actions": len(result.proposed_actions)})
            return []

        decision = _reply_decision(reply)
        if decision != "approved":
            logger.info("Sentinel actions rejected by user: %r", reply)
            append_log({"event": "actions_rejected", "reply": reply})
            return []

        logger.info("Sentinel actions approved — executing %d actions", len(result.proposed_actions))
        append_log({"event": "actions_approved", "reply": reply, "count": len(result.proposed_actions)})

        # Execute each approved action
        executed = []
        for action in result.proposed_actions:
            cmd = action.get("command", "")
            if not cmd:
                continue
            try:
                logger.info("Sentinel executing: %s", cmd)
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=60.0,
                )
                success = proc.returncode == 0
                action["success"] = success
                action["stdout"] = stdout.decode("utf-8", errors="replace")[:500]
                action["stderr"] = stderr.decode("utf-8", errors="replace")[:500]
                executed.append(action)
                if success:
                    logger.info("Sentinel action succeeded: %s", cmd)
                else:
                    logger.error(
                        "Sentinel action failed (rc=%d): %s — %s",
                        proc.returncode, cmd, stderr.decode("utf-8", errors="replace")[:200],
                    )
            except TimeoutError:
                logger.error("Sentinel action timed out: %s", cmd)
                action["success"] = False
                action["stderr"] = "Timed out after 60s"
                executed.append(action)
            except OSError as exc:
                logger.error("Sentinel action OS error: %s — %s", cmd, exc)

        return executed

    async def _create_observation(self, request: SentinelRequest, result: SentinelResult) -> None:
        """Create an observation for the ego to process."""
        import json
        import uuid

        obs_type = "sentinel_resolved" if result.resolved else "sentinel_escalated"
        priority = "low" if result.resolved else "critical"

        content = json.dumps({
            "trigger_source": request.trigger_source,
            "trigger_reason": request.trigger_reason,
            "tier": request.tier,
            "diagnosis": result.diagnosis[:500] if result.diagnosis else "",
            "actions_taken": result.actions_taken,
            "resolved": result.resolved,
            "duration_s": round(result.duration_s, 1),
            "session_id": result.session_id,
        })

        try:
            from genesis.db.crud import observations
            obs_id = f"sentinel-{uuid.uuid4().hex[:12]}"
            await observations.create(
                self._db,
                id=obs_id,
                source="sentinel",
                type=obs_type,
                content=content,
                priority=priority,
                created_at=datetime.now(UTC).isoformat(),
            )
            result.observation_id = obs_id
            logger.info("Sentinel observation created: %s (%s)", obs_id, obs_type)
        except Exception:
            logger.error("Failed to create sentinel observation", exc_info=True)

    def _backoff_ready(self, pattern: str) -> tuple[bool, str]:
        """Check whether this pattern is allowed to dispatch right now.

        Returns (ready, reason_if_not_ready). Escalated patterns are never
        ready until cleared by a resolved dispatch or process restart.
        Rejected patterns are suppressed until their rejection window expires.
        """
        # Check persistent rejection window (survives restarts)
        rejected_until = self._state.rejected_patterns.get(pattern)
        if rejected_until:
            now_iso = datetime.now(UTC).isoformat()
            if now_iso < rejected_until:
                return (
                    False,
                    f"Pattern {pattern!r} rejected until {rejected_until}",
                )
            # Window expired — clear the rejection
            del self._state.rejected_patterns[pattern]
            save_state(self._state)

        if pattern in self._escalated_patterns:
            since = self._escalated_patterns[pattern]
            return (
                False,
                f"Pattern {pattern!r} escalated to user at {since} — awaiting intervention",
            )

        attempts = self._pattern_attempts.get(pattern, [])
        count = len(attempts)

        # Escalation threshold reached: not "ready" per se — the dispatcher
        # handles escalation separately from the normal backoff gate. We
        # return ready=True so _gated_dispatch can call _escalate_pattern
        # instead of _execute_dispatch. The attempt counter check there
        # distinguishes the two.
        if count >= _ESCALATE_AT_ATTEMPT - 1:
            return True, f"escalation threshold reached (attempt {count + 1})"

        # First attempt for this pattern is always ready (schedule index 0 == 0s)
        if count == 0:
            return True, "first attempt for this pattern"

        # Subsequent attempts: need to wait per backoff schedule
        required_wait = _BACKOFF_SCHEDULE_S[count]
        elapsed = time.monotonic() - attempts[-1]
        if elapsed < required_wait:
            remaining_min = (required_wait - elapsed) / 60
            return (
                False,
                f"Pattern {pattern!r} in backoff: {remaining_min:.1f}m remaining "
                f"(attempt {count + 1}, waited {elapsed / 60:.1f}m of {required_wait / 60:.0f}m)",
            )
        return True, f"backoff cleared (attempt {count + 1})"

    async def _escalate_pattern(
        self, request: SentinelRequest, pattern: str, attempt_number: int,
    ) -> SentinelResult:
        """Post an escalation message and hold the pattern.

        Called when a pattern has burned through its backoff attempts and
        still isn't resolved. The Sentinel stops trying on its own and
        asks the user for help.

        Until Part 5 (alert lifecycle manager) wires interactive buttons,
        this is fire-and-forget: the message goes out, the pattern is
        marked as escalated, and subsequent attempts at the same pattern
        are suppressed until process restart.
        """
        now_iso = datetime.now(UTC).isoformat()
        self._escalated_patterns[pattern] = now_iso

        # "Attempts" here means dispatch-gate triggers, which includes both
        # CC sessions that ran-and-failed AND approvals you rejected. Either
        # way, the pattern is still active after N passes through the gate,
        # so the right move is to stop auto-responding and wait for you.
        prior = attempt_number - 1
        message = (
            f"🛡️ <b>Sentinel: I'm stuck.</b>\n\n"
            f"This issue has triggered {prior} dispatch attempts and it's still active. "
            f"I'm going to stop auto-responding to this pattern and wait for you.\n\n"
            f"<b>Pattern:</b> <code>{pattern}</code>\n"
            f"<b>Last reason:</b> {request.trigger_reason}\n\n"
            f"When you want me to resume, restart Genesis or clear the escalation."
        )

        # Fire-and-forget outreach. We don't wait for a reply — the
        # escalated_patterns dict holds the suppression until restart.
        if self._outreach_pipeline is not None:
            try:
                from genesis.outreach.pipeline import OutreachCategory, OutreachRequest

                await self._outreach_pipeline.submit_raw(
                    message,
                    OutreachRequest(
                        category=OutreachCategory.BLOCKER,
                        topic=f"Sentinel escalated: {pattern}",
                        context=message,
                        salience_score=1.0,
                        signal_type="sentinel_escalation",
                        source_id=f"sentinel-escalation:{pattern}:{int(time.time())}",
                    ),
                )
            except Exception:
                logger.error("Sentinel escalation message failed to send", exc_info=True)

        logger.warning(
            "Sentinel escalated pattern %r after %d attempts — auto-dispatch suppressed",
            pattern, attempt_number - 1,
        )
        append_log({
            "event": "pattern_escalated",
            "pattern": pattern,
            "attempt_count": attempt_number - 1,
            "trigger_source": request.trigger_source,
            "trigger_reason": request.trigger_reason,
        })

        return SentinelResult(
            dispatched=False,
            reason=(
                f"Pattern {pattern!r} escalated to user after "
                f"{attempt_number - 1} failed attempts"
            ),
        )

    async def check_fire_alarms(self) -> SentinelResult | None:
        """Check for fire alarm conditions and dispatch if warranted.

        Called from the awareness loop every tick. Returns None if no
        alarms detected, or a SentinelResult if dispatched.

        2-of-N debouncing: the alarms we act on are only those that have
        appeared in ≥_ALARM_CONFIRMATION_COUNT of the last _ALARM_RING_SIZE
        ticks. Single-tick flaps never wake the Sentinel.
        """
        if self._health_data is None:
            return None

        try:
            from genesis.mcp.health_mcp import _impl_health_alerts
            alerts = await _impl_health_alerts(active_only=True)
        except Exception:
            logger.debug("Failed to query health alerts for fire alarm check", exc_info=True)
            return None

        alarms = classify_alerts(alerts or [])
        current_ids = {a.alert_id for a in alarms}

        # Update the ring buffer with this tick's alarm ids (always — even
        # if empty, an empty set is data that confirms absence).
        self._recent_alarm_sets.append(current_ids)

        # If alarms have cleared while we're waiting for approval, cancel
        # the pending approval and go back to HEALTHY.
        if not alarms and self._state.state in (
            SentinelState.AWAITING_DISPATCH_APPROVAL,
            SentinelState.AWAITING_ACTION_APPROVAL,
        ):
            request_id = self._state.pending_request_id
            if request_id and self._approval_gate is not None:
                try:
                    await self._approval_gate.resolve_request(
                        request_id, decision="cancelled", resolved_by="alarm_cleared",
                    )
                except Exception:
                    logger.warning("Failed to cancel sentinel approval %s", request_id, exc_info=True)
            logger.info("Alarms cleared — cancelling pending sentinel approval %s", request_id)
            self._state.clear_pending()
            self._state.transition(SentinelState.HEALTHY, reason="alarms cleared while awaiting approval")
            save_state(self._state)
            return None

        if not alarms:
            return None

        # 2-of-N debounce: only consider alarms that appear in at least
        # _ALARM_CONFIRMATION_COUNT of the recent ring buffer entries.
        confirmed_ids = {
            aid for aid in current_ids
            if sum(1 for s in self._recent_alarm_sets if aid in s)
            >= _ALARM_CONFIRMATION_COUNT
        }
        if not confirmed_ids:
            logger.debug(
                "Sentinel: %d alarm(s) present but none confirmed by 2-of-%d debounce yet",
                len(alarms), _ALARM_RING_SIZE,
            )
            return None

        confirmed_alarms = [a for a in alarms if a.alert_id in confirmed_ids]
        tier = worst_tier(confirmed_alarms)

        # Tier 3 alarms are handled by reflexes only — don't wake the Sentinel
        if tier is not None and tier >= 3:
            return None

        # Tier 1 or 2: wake the Sentinel
        return await self.dispatch(SentinelRequest(
            trigger_source="fire_alarm",
            trigger_reason=(
                f"Tier {tier} alarm: {confirmed_alarms[0].message}"
                if confirmed_alarms else "unknown"
            ),
            tier=tier,
            alarms=confirmed_alarms,
        ))

    async def escalate_direct(
        self,
        *,
        trigger_source: str,
        tier: int,
        reason: str,
        context: dict[str, Any] | None = None,
    ) -> SentinelResult:
        """Direct escalation from another subsystem (e.g., GuardianWatchdog).

        Bypasses fire alarm classification — the caller has already determined
        this needs the Sentinel.
        """
        return await self.dispatch(SentinelRequest(
            trigger_source=trigger_source,
            trigger_reason=reason,
            tier=tier,
            context=context or {},
        ))

    @property
    def state(self) -> SentinelStateData:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._active_session_id is not None
