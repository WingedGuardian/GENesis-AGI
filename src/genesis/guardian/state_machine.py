"""Confirmation state machine — HOST-SIDE. Multi-step failure verification.

States:
  HEALTHY → SIGNAL_DROPPED (any signal fails)
  SIGNAL_DROPPED → HEALTHY (next check all OK — transient blip)
  SIGNAL_DROPPED → CONFIRMING (still down after recheck_delay_s)
  CONFIRMING → HEALTHY (signals recover)
  CONFIRMING → SURVEYING (max_recheck_attempts exceeded, >= required_failed_signals)
  SURVEYING → CONFIRMED_DEAD (diagnosis confirms failure)
  SURVEYING → HEALTHY (false alarm resolved during survey)
  CONFIRMED_DEAD → RECOVERING
  RECOVERING → RECOVERED (recovery action complete)
  RECOVERED → HEALTHY (post-recovery verification passes)
  RECOVERED → CONFIRMED_DEAD (verification fails — escalate)
  PAUSED (special mode when Genesis is paused)

The 30s recheck delay absorbs normal bridge/AZ restarts (~10-15s).
Heartbeat-only failure enters CONFIRMING with only 1 signal down.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from genesis.guardian.config import GuardianConfig
from genesis.guardian.health_signals import HealthSnapshot

logger = logging.getLogger(__name__)


class GuardianState(StrEnum):
    """Guardian state machine states."""

    HEALTHY = "healthy"
    SIGNAL_DROPPED = "signal_dropped"
    CONFIRMING = "confirming"
    SURVEYING = "surveying"
    CONTACTING_GENESIS = "contacting_genesis"
    AWAITING_SELF_HEAL = "awaiting_self_heal"
    CONFIRMED_DEAD = "confirmed_dead"
    RECOVERING = "recovering"
    RECOVERED = "recovered"
    PAUSED = "paused"


@dataclass
class StateData:
    """Persistent state between timer invocations."""

    current_state: GuardianState = GuardianState.HEALTHY
    consecutive_failures: int = 0
    recheck_count: int = 0
    first_failure_at: str | None = None
    last_healthy_at: str | None = None
    last_check_at: str | None = None
    recovery_attempts: int = 0
    last_restart_count: int = 0  # Track NRestarts for delta detection
    signal_history: list[dict] = field(default_factory=list)
    paused_since: str | None = None
    last_pause_reminder_at: str | None = None
    dialogue_sent_at: str | None = None
    dialogue_eta_s: int = 0
    dialogue_action: str | None = None
    cc_unavailable_since: str | None = None
    last_cc_unavailable_alert_at: str | None = None
    auto_reset_count: int = 0  # Oscillation guard for confirmed_dead timeout

    def to_dict(self) -> dict:
        return {
            "current_state": self.current_state.value,
            "consecutive_failures": self.consecutive_failures,
            "recheck_count": self.recheck_count,
            "first_failure_at": self.first_failure_at,
            "last_healthy_at": self.last_healthy_at,
            "last_check_at": self.last_check_at,
            "recovery_attempts": self.recovery_attempts,
            "last_restart_count": self.last_restart_count,
            "signal_history": self.signal_history[-20:],
            "paused_since": self.paused_since,
            "last_pause_reminder_at": self.last_pause_reminder_at,
            "dialogue_sent_at": self.dialogue_sent_at,
            "dialogue_eta_s": self.dialogue_eta_s,
            "dialogue_action": self.dialogue_action,
            "cc_unavailable_since": self.cc_unavailable_since,
            "last_cc_unavailable_alert_at": self.last_cc_unavailable_alert_at,
            "auto_reset_count": self.auto_reset_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StateData:
        try:
            state = GuardianState(data.get("current_state", "healthy"))
        except ValueError:
            state = GuardianState.HEALTHY
        return cls(
            current_state=state,
            consecutive_failures=data.get("consecutive_failures", 0),
            recheck_count=data.get("recheck_count", 0),
            first_failure_at=data.get("first_failure_at"),
            last_healthy_at=data.get("last_healthy_at"),
            last_check_at=data.get("last_check_at"),
            recovery_attempts=data.get("recovery_attempts", 0),
            last_restart_count=data.get("last_restart_count", 0),
            signal_history=data.get("signal_history", []),
            paused_since=data.get("paused_since"),
            last_pause_reminder_at=data.get("last_pause_reminder_at"),
            dialogue_sent_at=data.get("dialogue_sent_at"),
            dialogue_eta_s=data.get("dialogue_eta_s", 0),
            dialogue_action=data.get("dialogue_action"),
            cc_unavailable_since=data.get("cc_unavailable_since"),
            last_cc_unavailable_alert_at=data.get("last_cc_unavailable_alert_at"),
            auto_reset_count=data.get("auto_reset_count", 0),
        )


@dataclass(frozen=True)
class Transition:
    """Result of a state machine transition."""

    old_state: GuardianState
    new_state: GuardianState
    reason: str
    action_needed: bool = False

    @property
    def changed(self) -> bool:
        return self.old_state != self.new_state


class ConfirmationStateMachine:
    """Confirmation-first state machine for Guardian health monitoring.

    Designed to avoid false positives: a single dropped signal doesn't
    trigger recovery. Multiple checks over time are required before the
    Guardian considers Genesis truly dead.
    """

    def __init__(self, config: GuardianConfig) -> None:
        self._config = config
        self._state = StateData()

    @property
    def state(self) -> StateData:
        return self._state

    @property
    def current_state(self) -> GuardianState:
        return self._state.current_state

    def load_state(self, path: Path) -> None:
        """Load state from disk. Starts fresh on any error (F3)."""
        try:
            if path.exists():
                data = json.loads(path.read_text())
                self._state = StateData.from_dict(data)
                logger.debug("Loaded guardian state: %s", self._state.current_state)
            else:
                logger.info("No state file found, starting fresh")
        except (json.JSONDecodeError, OSError, TypeError, KeyError) as exc:
            logger.warning("State file corrupted, starting fresh: %s", exc)
            self._state = StateData()

    def save_state(self, path: Path) -> None:
        """Persist state to disk atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._state.to_dict(), indent=2))
            tmp.replace(path)
        except OSError as exc:
            logger.error("Failed to save state: %s", exc, exc_info=True)

    def process(self, snapshot: HealthSnapshot) -> Transition:
        """Process a health snapshot and return the state transition.

        This is the core state machine logic. Called once per timer
        invocation with the collected health snapshot.
        """
        now = datetime.now(UTC).isoformat()
        self._state.last_check_at = now

        # Record snapshot in history (compact form)
        self._state.signal_history.append({
            "at": now,
            "all_alive": snapshot.all_alive,
            "failed": [s.name for s in snapshot.failed_signals],
            "warnings": [s.name for s in snapshot.suspicious_warnings],
        })
        # Keep last 20
        self._state.signal_history = self._state.signal_history[-20:]

        old_state = self._state.current_state

        # Handle pause state first
        if snapshot.pause_state.paused:
            return self._handle_paused(snapshot, old_state, now)

        # If we were paused and now unpaused, resume normal monitoring
        if old_state == GuardianState.PAUSED:
            self._state.current_state = GuardianState.HEALTHY
            self._state.paused_since = None
            return Transition(
                old_state=old_state,
                new_state=GuardianState.HEALTHY,
                reason="Genesis unpaused, resuming normal monitoring",
            )

        # Main state machine transitions
        if old_state == GuardianState.HEALTHY:
            return self._from_healthy(snapshot, now)
        elif old_state == GuardianState.SIGNAL_DROPPED:
            return self._from_signal_dropped(snapshot, now)
        elif old_state == GuardianState.CONFIRMING:
            return self._from_confirming(snapshot, now)
        elif old_state == GuardianState.SURVEYING:
            # SURVEYING is handled externally (check.py contacts Genesis)
            return Transition(
                old_state=old_state,
                new_state=GuardianState.SURVEYING,
                reason="survey in progress",
            )
        elif old_state == GuardianState.CONTACTING_GENESIS:
            # Handled externally by check.py dialogue logic
            if snapshot.all_alive:
                self._reset_to_healthy(now)
                return Transition(
                    old_state=old_state,
                    new_state=GuardianState.HEALTHY,
                    reason="Genesis recovered during dialogue",
                )
            return Transition(
                old_state=old_state,
                new_state=GuardianState.CONTACTING_GENESIS,
                reason="dialogue in progress",
            )
        elif old_state == GuardianState.AWAITING_SELF_HEAL:
            # Genesis said it's handling it — check if it succeeded
            if snapshot.all_alive:
                self._reset_to_healthy(now)
                return Transition(
                    old_state=old_state,
                    new_state=GuardianState.HEALTHY,
                    reason="Genesis self-healed successfully",
                )
            # Check if ETA has expired
            if self._dialogue_eta_expired():
                self._state.current_state = GuardianState.CONFIRMED_DEAD
                return Transition(
                    old_state=old_state,
                    new_state=GuardianState.CONFIRMED_DEAD,
                    reason="Genesis self-heal ETA expired, still unhealthy",
                    action_needed=True,
                )
            return Transition(
                old_state=old_state,
                new_state=GuardianState.AWAITING_SELF_HEAL,
                reason=f"waiting for Genesis self-heal: {self._state.dialogue_action}",
            )
        elif old_state == GuardianState.CONFIRMED_DEAD:
            if snapshot.all_alive:
                # Container recovered on its own (services restarted,
                # slow boot, independent fix, etc.)
                self._reset_to_healthy(now)
                self._state.auto_reset_count = 0  # Genuine recovery clears oscillation guard
                return Transition(
                    old_state=old_state,
                    new_state=GuardianState.HEALTHY,
                    reason="all signals healthy — auto-recovered from confirmed_dead",
                )

            # Auto-reset timeout: if stuck in confirmed_dead too long,
            # reset to HEALTHY to re-evaluate from scratch. Prevents
            # permanent stuck state when the root cause (e.g., auth
            # blocking probes) has been fixed but a secondary signal
            # remains flaky.
            timeout_s = self._config.confirmation.confirmed_dead_timeout_s
            max_resets = self._config.confirmation.max_auto_resets
            if (
                self._state.first_failure_at
                and self._state.auto_reset_count < max_resets
            ):
                try:
                    first = datetime.fromisoformat(self._state.first_failure_at)
                    stuck_s = (datetime.now(UTC) - first).total_seconds()
                except (ValueError, TypeError):
                    stuck_s = 0.0
                if stuck_s > timeout_s:
                    self._state.auto_reset_count += 1
                    logger.warning(
                        "confirmed_dead timeout (%.0fs > %ds, reset %d/%d) — "
                        "auto-resetting to re-evaluate",
                        stuck_s, timeout_s,
                        self._state.auto_reset_count, max_resets,
                    )
                    self._reset_to_healthy(now)
                    return Transition(
                        old_state=old_state,
                        new_state=GuardianState.HEALTHY,
                        reason=f"auto-reset after {stuck_s:.0f}s in confirmed_dead "
                               f"(reset {self._state.auto_reset_count}/{max_resets})",
                    )

            return Transition(
                old_state=old_state,
                new_state=GuardianState.CONFIRMED_DEAD,
                reason="awaiting recovery",
                action_needed=True,
            )
        elif old_state == GuardianState.RECOVERING:
            return Transition(
                old_state=old_state,
                new_state=GuardianState.RECOVERING,
                reason="recovery in progress",
            )
        elif old_state == GuardianState.RECOVERED:
            return self._from_recovered(snapshot, now)
        else:
            # Unknown state — reset to healthy
            self._reset_to_healthy(now)
            return Transition(
                old_state=old_state,
                new_state=GuardianState.HEALTHY,
                reason=f"unknown state {old_state}, reset to healthy",
            )

    def _handle_paused(
        self, snapshot: HealthSnapshot, old_state: GuardianState, now: str,
    ) -> Transition:
        """Handle Genesis being paused — infrastructure-only monitoring."""
        if old_state != GuardianState.PAUSED:
            self._state.current_state = GuardianState.PAUSED
            self._state.paused_since = now
            return Transition(
                old_state=old_state,
                new_state=GuardianState.PAUSED,
                reason=f"Genesis paused: {snapshot.pause_state.reason or 'no reason'}",
            )

        # Already paused — check infrastructure probes
        # Container must exist and be reachable even when paused
        container_signal = snapshot.signals.get("container_exists")
        if container_signal and not container_signal.alive:
            # Infrastructure failure while paused — still alarm
            self._state.current_state = GuardianState.SIGNAL_DROPPED
            self._state.first_failure_at = now
            self._state.consecutive_failures = 1
            return Transition(
                old_state=old_state,
                new_state=GuardianState.SIGNAL_DROPPED,
                reason="container down while Genesis paused",
                action_needed=True,
            )

        # Check for long pause reminder (fires at most once per reminder period)
        if self._state.paused_since:
            try:
                paused_dt = datetime.fromisoformat(self._state.paused_since)
                hours = (datetime.now(UTC) - paused_dt).total_seconds() / 3600
                reminder_hours = self._config.confirmation.pause_reminder_hours

                if hours > reminder_hours:
                    # Only send if we haven't reminded recently
                    should_remind = True
                    if self._state.last_pause_reminder_at:
                        last_reminder = datetime.fromisoformat(
                            self._state.last_pause_reminder_at,
                        )
                        since_last = (datetime.now(UTC) - last_reminder).total_seconds() / 3600
                        should_remind = since_last >= reminder_hours

                    if should_remind:
                        self._state.last_pause_reminder_at = now
                        return Transition(
                            old_state=old_state,
                            new_state=GuardianState.PAUSED,
                            reason=f"paused for {hours:.0f}h — reminder",
                            action_needed=True,
                        )
            except (ValueError, TypeError):
                pass

        return Transition(
            old_state=old_state,
            new_state=GuardianState.PAUSED,
            reason="paused, infrastructure OK",
        )

    def _from_healthy(self, snapshot: HealthSnapshot, now: str) -> Transition:
        """Transition from HEALTHY state."""
        if snapshot.all_alive:
            self._state.last_healthy_at = now
            # Clear auto-reset oscillation guard — system has been healthy
            # for a full check cycle, so any prior incident is resolved.
            if self._state.auto_reset_count > 0:
                self._state.auto_reset_count = 0
            return Transition(
                old_state=GuardianState.HEALTHY,
                new_state=GuardianState.HEALTHY,
                reason="all probes healthy",
            )

        # Signal(s) dropped
        self._state.current_state = GuardianState.SIGNAL_DROPPED
        self._state.first_failure_at = now
        self._state.consecutive_failures = 1
        self._state.recheck_count = 0

        failed_names = [s.name for s in snapshot.failed_signals]
        return Transition(
            old_state=GuardianState.HEALTHY,
            new_state=GuardianState.SIGNAL_DROPPED,
            reason=f"signals dropped: {', '.join(failed_names)}",
        )

    def _from_signal_dropped(
        self, snapshot: HealthSnapshot, now: str,
    ) -> Transition:
        """Transition from SIGNAL_DROPPED — check if transient or persistent."""
        if snapshot.all_alive:
            # Transient blip — recovered
            self._reset_to_healthy(now)
            return Transition(
                old_state=GuardianState.SIGNAL_DROPPED,
                new_state=GuardianState.HEALTHY,
                reason="transient blip, all probes recovered",
            )

        # Still failing — advance to CONFIRMING
        self._state.current_state = GuardianState.CONFIRMING
        self._state.consecutive_failures += 1
        self._state.recheck_count = 1

        failed_names = [s.name for s in snapshot.failed_signals]
        return Transition(
            old_state=GuardianState.SIGNAL_DROPPED,
            new_state=GuardianState.CONFIRMING,
            reason=f"persistent failure: {', '.join(failed_names)}",
        )

    def _from_confirming(
        self, snapshot: HealthSnapshot, now: str,
    ) -> Transition:
        """Transition from CONFIRMING — recheck until confirmed or recovered."""
        if snapshot.all_alive:
            # Recovered during confirmation
            self._reset_to_healthy(now)
            return Transition(
                old_state=GuardianState.CONFIRMING,
                new_state=GuardianState.HEALTHY,
                reason="recovered during confirmation",
            )

        self._state.consecutive_failures += 1
        self._state.recheck_count += 1

        failed_count = len(snapshot.failed_signals)
        max_rechecks = self._config.confirmation.max_recheck_attempts
        required_signals = self._config.confirmation.required_failed_signals

        # Heartbeat-only failure is weighted more heavily
        heartbeat = snapshot.signals.get("heartbeat_canary")
        heartbeat_only_failure = (
            heartbeat is not None
            and not heartbeat.alive
            and failed_count == 1
        )

        # Check if we should escalate to SURVEYING
        enough_rechecks = self._state.recheck_count >= max_rechecks
        enough_signals = failed_count >= required_signals or heartbeat_only_failure

        # Handle bootstrapping — don't escalate on 503 within grace period
        if self._is_within_bootstrap_grace():
            return Transition(
                old_state=GuardianState.CONFIRMING,
                new_state=GuardianState.CONFIRMING,
                reason="within bootstrap grace period",
            )

        if enough_rechecks and enough_signals:
            self._state.current_state = GuardianState.SURVEYING
            failed_names = [s.name for s in snapshot.failed_signals]
            return Transition(
                old_state=GuardianState.CONFIRMING,
                new_state=GuardianState.SURVEYING,
                reason=(
                    f"confirmed: {failed_count} signals down "
                    f"after {self._state.recheck_count} rechecks "
                    f"({', '.join(failed_names)})"
                ),
                action_needed=True,
            )

        failed_names = [s.name for s in snapshot.failed_signals]
        return Transition(
            old_state=GuardianState.CONFIRMING,
            new_state=GuardianState.CONFIRMING,
            reason=(
                f"recheck {self._state.recheck_count}/{max_rechecks}, "
                f"{failed_count} signals down ({', '.join(failed_names)})"
            ),
        )

    def _from_recovered(
        self, snapshot: HealthSnapshot, now: str,
    ) -> Transition:
        """Transition from RECOVERED — verify recovery worked."""
        if snapshot.all_alive:
            self._reset_to_healthy(now)
            return Transition(
                old_state=GuardianState.RECOVERED,
                new_state=GuardianState.HEALTHY,
                reason="recovery verified — all probes healthy",
            )

        # Recovery failed — back to CONFIRMED_DEAD for escalation
        self._state.current_state = GuardianState.CONFIRMED_DEAD
        self._state.recovery_attempts += 1
        failed_names = [s.name for s in snapshot.failed_signals]
        return Transition(
            old_state=GuardianState.RECOVERED,
            new_state=GuardianState.CONFIRMED_DEAD,
            reason=(
                f"recovery verification failed — "
                f"{', '.join(failed_names)} still down "
                f"(attempt {self._state.recovery_attempts})"
            ),
            action_needed=True,
        )

    def _is_within_bootstrap_grace(self) -> bool:
        """Check if we're within the bootstrap grace period (F4)."""
        if not self._state.first_failure_at:
            return False
        try:
            first = datetime.fromisoformat(self._state.first_failure_at)
            elapsed = (datetime.now(UTC) - first).total_seconds()
            return elapsed < self._config.confirmation.bootstrap_grace_s
        except (ValueError, TypeError):
            return False

    def _reset_to_healthy(self, now: str) -> None:
        """Reset all failure tracking state."""
        self._state.current_state = GuardianState.HEALTHY
        self._state.consecutive_failures = 0
        self._state.recheck_count = 0
        self._state.first_failure_at = None
        self._state.last_healthy_at = now
        self._state.recovery_attempts = 0
        self._clear_dialogue_state()
        self.clear_cc_unavailable()

    # ── External state manipulation (called by recovery engine) ────────

    def _dialogue_eta_expired(self) -> bool:
        """Check if the self-heal ETA has passed."""
        if not self._state.dialogue_sent_at or not self._state.dialogue_eta_s:
            return True
        try:
            sent = datetime.fromisoformat(self._state.dialogue_sent_at)
            elapsed = (datetime.now(UTC) - sent).total_seconds()
            return elapsed > self._state.dialogue_eta_s
        except (ValueError, TypeError):
            return True

    def _clear_dialogue_state(self) -> None:
        """Clear dialogue tracking fields."""
        self._state.dialogue_sent_at = None
        self._state.dialogue_eta_s = 0
        self._state.dialogue_action = None

    # ── External state manipulation (called by check.py) ───────────────

    def set_surveying(self) -> None:
        """Advance to SURVEYING (called when diagnosis starts)."""
        self._state.current_state = GuardianState.SURVEYING

    def set_contacting_genesis(self) -> None:
        """Advance to CONTACTING_GENESIS (called when dialogue starts)."""
        self._state.current_state = GuardianState.CONTACTING_GENESIS
        self._state.dialogue_sent_at = datetime.now(UTC).isoformat()

    def set_awaiting_self_heal(self, action: str, eta_s: int) -> None:
        """Genesis acknowledged and is handling it. Wait for ETA."""
        self._state.current_state = GuardianState.AWAITING_SELF_HEAL
        self._state.dialogue_action = action
        self._state.dialogue_eta_s = eta_s

    def set_paused(self, reason: str = "") -> None:
        """Enter PAUSED state (called when Genesis requests stand-down)."""
        self._state.current_state = GuardianState.PAUSED
        self._state.paused_since = datetime.now(UTC).isoformat()
        self._clear_dialogue_state()

    def set_confirmed_dead(self) -> None:
        """Advance to CONFIRMED_DEAD (called after diagnosis)."""
        self._state.current_state = GuardianState.CONFIRMED_DEAD

    def set_recovering(self) -> None:
        """Advance to RECOVERING (called when recovery action starts)."""
        self._state.current_state = GuardianState.RECOVERING

    def set_recovered(self) -> None:
        """Advance to RECOVERED (called after recovery action completes)."""
        self._state.current_state = GuardianState.RECOVERED

    def set_cc_unavailable(self) -> None:
        """Record that CC diagnosis is unavailable (first occurrence)."""
        if not self._state.cc_unavailable_since:
            self._state.cc_unavailable_since = datetime.now(UTC).isoformat()

    def clear_cc_unavailable(self) -> None:
        """CC is back online — clear unavailability tracking."""
        self._state.cc_unavailable_since = None
        self._state.last_cc_unavailable_alert_at = None

    def record_cc_unavailable_alert(self) -> None:
        """Record that we sent a CC-unavailable alert."""
        self._state.last_cc_unavailable_alert_at = datetime.now(UTC).isoformat()

    def should_escalate(self) -> bool:
        """Check if we've exceeded max recovery attempts."""
        return self._state.recovery_attempts >= self._config.recovery.max_escalations
