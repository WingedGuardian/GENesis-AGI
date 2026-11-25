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


_DROP_ATTEMPT_KEYS = frozenset({"branch", "pinned_head", "tip", "at"})


def _drop_attempt(raw: object) -> dict | None:
    """A persisted drop attempt, or None when it is not one (a hand-edited or
    older state file)."""
    if (
        isinstance(raw, dict)
        and set(raw) == _DROP_ATTEMPT_KEYS
        and all(isinstance(v, str) for v in raw.values())
    ):
        return dict(raw)
    return None


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
    sentinel_state: str = ""  # Latest Sentinel state from dialogue response
    cc_unavailable_since: str | None = None
    last_cc_unavailable_alert_at: str | None = None
    down_alert_sent: bool = False  # GUARD-R2-01: one "down" alert per episode (no storm)
    auto_reset_count: int = 0  # Oscillation guard for confirmed_dead timeout
    last_recovery_at: str | None = None  # ISO8601 timestamp of last recovery attempt
    io_triage_attempts: int = 0  # Separate counter for IO_TRIAGE (low-risk, repeatable)
    snapshot_size_history: list[int] = field(default_factory=list)  # Last N snapshot sizes in bytes
    boot_id: str = ""  # kernel boot UUID when this state was last written; "" = unknown
    rebooted_at: str | None = None  # when this process last saw a host reboot (post-reboot grace)
    # The Guardian's last drop on `live` this episode (#2978): branch,
    # pinned_head, tip (`live` before it) and at. Read only with
    # recovery.live_engine on; kept through the auto-reset, a DENY and a failed
    # send; cleared when the episode ends (a healthy snapshot). While it is set,
    # an action that ran ends in RECOVERED, whose one transition to
    # CONFIRMED_DEAD lets the next diagnosis past the down-alert latch.
    live_drop_attempt: dict | None = None
    # On `live` with recovery.live_engine on (B2, #4841): this episode waits for
    # update.lock. Each tick probes the lock instead of staying quiet; the wait
    # ends with the episode. last_alert_at is written by stamp_alert alone.
    waiting_on_update_lock: bool = False
    waiting_since: str | None = None
    last_alert_at: str | None = None
    # Whether this wait has sent its frozen APPROVE gate (owner ruling 91998f4f):
    # the first unreadable read on a running container gates at once, then 2 h.
    # A field because last_alert_at cannot tell a held re-alert from a gate.
    frozen_gate_sent: bool = False

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
            "sentinel_state": self.sentinel_state,
            "cc_unavailable_since": self.cc_unavailable_since,
            "last_cc_unavailable_alert_at": self.last_cc_unavailable_alert_at,
            "down_alert_sent": self.down_alert_sent,
            "auto_reset_count": self.auto_reset_count,
            "last_recovery_at": self.last_recovery_at,
            "io_triage_attempts": self.io_triage_attempts,
            "snapshot_size_history": self.snapshot_size_history[-5:],
            "boot_id": self.boot_id,
            "rebooted_at": self.rebooted_at,
            "live_drop_attempt": self.live_drop_attempt,
            "waiting_on_update_lock": self.waiting_on_update_lock,
            "waiting_since": self.waiting_since,
            "last_alert_at": self.last_alert_at,
            "frozen_gate_sent": self.frozen_gate_sent,
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
            sentinel_state=data.get("sentinel_state", ""),
            cc_unavailable_since=data.get("cc_unavailable_since"),
            last_cc_unavailable_alert_at=data.get("last_cc_unavailable_alert_at"),
            down_alert_sent=data.get("down_alert_sent", False),
            auto_reset_count=data.get("auto_reset_count", 0),
            last_recovery_at=data.get("last_recovery_at"),
            io_triage_attempts=data.get("io_triage_attempts", 0),
            snapshot_size_history=data.get("snapshot_size_history", []),
            boot_id=str(data.get("boot_id") or ""),
            rebooted_at=data.get("rebooted_at") or None,
            live_drop_attempt=_drop_attempt(data.get("live_drop_attempt")),
            waiting_on_update_lock=data.get("waiting_on_update_lock") is True,
            waiting_since=_text(data.get("waiting_since")),
            last_alert_at=_text(data.get("last_alert_at")),
            frozen_gate_sent=data.get("frozen_gate_sent") is True,
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _usable(raw: str | None, now: datetime) -> datetime | None:
    """A persisted stamp, or None when it is missing, unparseable, naive or in
    the future: each would freeze a countdown that reads it."""
    try:
        when = datetime.fromisoformat(raw or "")
    except (ValueError, TypeError):
        return None
    return when if when.tzinfo is not None and when <= now else None


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
        self._path: Path | None = None  # what load_state read: persist() writes there

    @property
    def state(self) -> StateData:
        return self._state

    @property
    def current_state(self) -> GuardianState:
        return self._state.current_state

    def load_state(self, path: Path) -> None:
        """Load state from disk. Starts fresh on any error (F3)."""
        self._path = path
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

    def save_state(self, path: Path) -> bool:
        """Persist state to disk atomically. True once it is on disk."""
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._state.to_dict(), indent=2))
            tmp.replace(path)
        except OSError as exc:
            logger.error("Failed to save state: %s", exc, exc_info=True)
            return False
        return True

    @property
    def path(self) -> Path | None:
        return self._path

    def persist(self) -> bool:
        """Save to the path load_state read, mid-tick (#5007): a tick killed
        later still leaves this on disk. False when it could not be saved; no
        path read (tests): nothing written, True."""
        return self._path is None or self.save_state(self._path)

    def recovery_backoff_remaining_s(self, action: str = "") -> float:
        """Seconds remaining before next recovery attempt is allowed.

        IO_TRIAGE has a separate counter (low-risk, one kill per cycle).
        All other actions share the main counter with exponential backoff:
        100s, 300s, 900s (~2min, 5min, 15min).

        Returns 0 if no backoff needed, inf if already escalated.
        """
        if action == "IO_TRIAGE":
            max_io = getattr(self._config.recovery, "max_io_triage_attempts", 5)
            attempts = self._state.io_triage_attempts
            if attempts == 0:
                return 0.0
            if attempts >= max_io:
                return float("inf")
            # Shorter backoff for IO_TRIAGE: 30s base, 2x multiplier
            backoff_s = 30.0 * (2.0 ** (attempts - 1))
        else:
            attempts = self._state.recovery_attempts
            if attempts == 0:
                return 0.0
            if attempts >= self._config.recovery.max_escalations:
                return float("inf")
            backoff_s = 100.0 * (3.0 ** (attempts - 1))

        elapsed = (datetime.now(UTC) - self._recovery_stamp()).total_seconds()
        return max(0.0, backoff_s - elapsed)

    def _recovery_stamp(self) -> datetime:
        """last_recovery_at, readable. A missing, unparseable, naive or future
        stamp would freeze the backoff at its full length on every tick (a held
        or deferred action that never runs), so it is reset to now and the
        backoff counts down once from there; the state is saved every cycle."""
        now = datetime.now(UTC)
        raw = self._state.last_recovery_at
        last = _usable(raw, now)
        if last is not None:
            return last
        logger.warning("last_recovery_at %r is unusable; counting the backoff from now", raw)
        self._state.last_recovery_at = now.isoformat()
        return now

    def record_recovery_attempt(self, action: str = "") -> None:
        """Record that a recovery attempt was made (for backoff tracking).

        IO_TRIAGE increments its own counter. All other actions increment
        the main counter. Both share the last_recovery_at timestamp.
        """
        self._state.last_recovery_at = datetime.now(UTC).isoformat()
        if action == "IO_TRIAGE":
            self._state.io_triage_attempts += 1
        else:
            self._state.recovery_attempts += 1

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
        if snapshot.all_alive:  # the episode is over: its drop needs no naming
            self._state.live_drop_attempt = None

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
            # Event-driven: check Sentinel state, not wall-clock timeout
            if self._should_proceed_past_self_heal():
                reason = (
                    f"Sentinel state '{self._state.sentinel_state or 'unreachable'}' "
                    f"— proceeding to diagnosis"
                )
                self._state.current_state = GuardianState.CONFIRMED_DEAD
                return Transition(
                    old_state=old_state,
                    new_state=GuardianState.CONFIRMED_DEAD,
                    reason=reason,
                    action_needed=True,
                )
            return Transition(
                old_state=old_state,
                new_state=GuardianState.AWAITING_SELF_HEAL,
                reason=(
                    f"waiting for Sentinel ({self._state.sentinel_state}): "
                    f"{self._state.dialogue_action}"
                ),
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
            # Not while waiting for update.lock (B2): check.py probes the lock each
            # tick, and a reset would re-diagnose into the same wait and alert again.
            if (
                self._state.first_failure_at
                and self._state.auto_reset_count < max_resets
                and self._state.waiting_on_update_lock is not True
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
        elif old_state in (GuardianState.RECOVERING, GuardianState.RECOVERED):
            # Each tick is a fresh process, so RECOVERING on disk is a killed
            # tick's: an action whose outcome was not seen (#5007).
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

        # After a `live` drop attempt (#2978) the next decision waits out the
        # backoff here: CONFIRMED_DEAD would auto-reset, or stay latched. At
        # max_escalations the backoff never ends, so it goes on to the escalation.
        # RECOVERED has no auto-reset; the hold ends because the backoff always
        # counts down (an unusable stamp restarts it from now, _recovery_stamp).
        backoff = self.recovery_backoff_remaining_s()
        drop = self._config.recovery.live_engine and self._state.live_drop_attempt
        if drop and 0 < backoff < float("inf"):
            return Transition(
                old_state=GuardianState.RECOVERED,
                new_state=GuardianState.RECOVERED,
                reason=f"after a drop attempt: {backoff:.0f}s of backoff before the next decision",
            )
        # Recovery failed — back to CONFIRMED_DEAD for escalation.
        # recovery_attempts already incremented by record_recovery_attempt().
        self._state.current_state = GuardianState.CONFIRMED_DEAD
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
        """Check if we're within the bootstrap grace period (F4).

        An episode whose first failure falls within ``post_reboot_grace_s`` of
        a detected host reboot gets that longer grace: a cold boot of the
        container stack takes far longer than a service restart.
        """
        if not self._state.first_failure_at:
            return False
        try:
            first = datetime.fromisoformat(self._state.first_failure_at)
            elapsed = (datetime.now(UTC) - first).total_seconds()
        except (ValueError, TypeError):
            return False
        grace = self._config.confirmation.bootstrap_grace_s
        post = self._config.confirmation.post_reboot_grace_s
        if self._state.rebooted_at and post > grace:
            try:
                since_boot = (first - datetime.fromisoformat(self._state.rebooted_at)).total_seconds()
            except (ValueError, TypeError):
                since_boot = None
            if since_boot is not None and -60 <= since_boot < post:
                grace = post
        return elapsed < grace

    def reset_if_rebooted(self, current_boot_id: str | None) -> bool:
        """Drop a stale episode when the host has rebooted since it was written.

        Returns True if a reset happened.

        The escalation ladder must never resume mid-climb across a reboot. The
        state file survives (`load_state` restores it verbatim, fresh only on
        corruption) and the 300s bootstrap grace is consulted from exactly one
        transition — CONFIRMING->SURVEYING — so past CONFIRMING the grace is
        never reached, and inside CONFIRMING it is computed from the persisted
        PRE-reboot ``first_failure_at`` and has already expired on arrival.
        Resetting to HEALTHY re-arms the grace that already works, through the
        normal HEALTHY->SIGNAL_DROPPED->CONFIRMING path.

        This deliberately does NOT call ``_reset_to_healthy``: that zeroes
        ``recovery_attempts``, ``io_triage_attempts`` and ``last_recovery_at``,
        which are three of the values that must survive.

        The budgets are preserved because a reboot is not evidence the incident
        is over, and the Guardian cannot tell a reboot it provoked from an
        unrelated one. Refunding the escalation budget on every reboot would turn
        a bounded 3-rung ladder into an unbounded loop on a box that is
        reboot-cycling — which is exactly the box where reboots get observed.
        (The gateway's manual ``reset-state`` verb DOES zero them, correctly: a
        human has looked at the machine and decided the incident is over.)

        Fails OPEN on an unknown boot id: a false positive silently discards a
        live episode, which is worse than carrying a stale one for another tick.
        """
        if not current_boot_id:
            return False
        stored = self._state.boot_id
        # Adopt, don't reset, when the state predates this field — otherwise the
        # first tick after deploying this would wipe a live episode everywhere.
        if not stored:
            self._state.boot_id = current_boot_id
            return False
        if stored == current_boot_id:
            return False

        was_paused = self._state.current_state is GuardianState.PAUSED
        logger.warning(
            "Host rebooted since this state was written (boot %s -> %s) — %s; "
            "recovery budgets are preserved",
            stored, current_boot_id,
            "staying PAUSED (user sovereignty), clearing episode tracking only"
            if was_paused else
            f"dropping the '{self._state.current_state.value}' episode and "
            "re-arming the bootstrap grace",
        )
        # PAUSED is not a rung on the ladder: process() re-derives it from the
        # snapshot every tick, and forcing it to HEALTHY here would make
        # _handle_paused rewrite paused_since, restarting the long-pause reminder
        # clock on every reboot.
        if self._state.current_state is not GuardianState.PAUSED:
            self._state.current_state = GuardianState.HEALTHY
        self._state.consecutive_failures = 0
        self._state.recheck_count = 0
        self._state.first_failure_at = None
        # Stale pre-reboot signals would otherwise be quoted into post-reboot
        # alert bodies (check.py reads signal_history[-5:] at three alert sites)
        # as if the episode were contiguous. Nothing holds a live reference to
        # this list — process() rebinds it every tick — so clear() is for
        # symmetry with snapshot_size_history, which IS aliased, not because it
        # has to be in place here.
        self._state.signal_history.clear()
        self._clear_dialogue_state()
        self.clear_cc_unavailable()
        self.end_wait()
        self._state.boot_id = current_boot_id
        self._state.rebooted_at = datetime.now(UTC).isoformat()
        return True

    def _reset_to_healthy(self, now: str) -> None:
        """Reset all failure tracking state."""
        self._state.current_state = GuardianState.HEALTHY
        self._state.consecutive_failures = 0
        self._state.recheck_count = 0
        self._state.first_failure_at = None
        self._state.last_healthy_at = now
        self._state.recovery_attempts = 0
        self._state.io_triage_attempts = 0
        self._state.last_recovery_at = None
        self._clear_dialogue_state()
        self.clear_cc_unavailable()
        self.end_wait()

    # ── External state manipulation (called by recovery engine) ────────

    def _should_proceed_past_self_heal(self) -> bool:
        """Check if Guardian should stop waiting for Sentinel.

        Event-driven standing — NO wall-clock timeout. Proceeds ONLY when:
        - Sentinel state is empty (Genesis unreachable, no state reported)
        - Sentinel transitioned to "escalated" (explicitly gave up)
        - Sentinel returned to "healthy" (thinks it fixed it — but probes
          say otherwise, so Guardian should proceed to its own diagnosis)

        NEVER proceeds based on elapsed time. If Sentinel is investigating
        or awaiting approval for 8 hours, Guardian waits 8 hours. User
        sovereignty is absolute.
        """
        state = self._state.sentinel_state
        if not state:
            return True  # No state = Genesis unreachable
        if state == "escalated":
            return True  # Sentinel explicitly gave up
        # "healthy" = Sentinel thinks it fixed it, but probes still fail
        return state == "healthy"

    def _clear_dialogue_state(self) -> None:
        """Clear dialogue tracking fields."""
        self._state.dialogue_sent_at = None
        self._state.dialogue_eta_s = 0
        self._state.dialogue_action = None
        self._state.sentinel_state = ""

    def update_sentinel_state(self, state: str) -> None:
        """Update the latest Sentinel state (called by check.py on re-query)."""
        self._state.sentinel_state = state

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

    def mark_down_alert_sent(self) -> None:
        """Record that the one-per-episode 'down' alert has been sent (GUARD-R2-01).

        Gates re-diagnosis/re-alert in CONFIRMED_DEAD so a downed-and-stays-down
        container produces ONE down notification, not a 30s storm. Persisted in
        state.json (each tick is a fresh oneshot process). Cleared only on genuine
        recovery — NOT in _reset_to_healthy, which auto-reset also calls.
        """
        self._state.down_alert_sent = True

    def clear_down_alert_sent(self) -> None:
        """Clear the down-alert flag — called by check.py on genuine recovery only.
        The episode's wait on update.lock ends with it."""
        self._state.down_alert_sent = False
        self.end_wait()

    # ── the wait on update.lock (B2, #4841) ─────────────────────────────────

    def mark_waiting(self) -> None:
        """This episode waits for update.lock; a wait already running keeps its start."""
        if not (self._state.waiting_on_update_lock is True and self._state.waiting_since):
            self._state.waiting_since = datetime.now(UTC).isoformat()
        self._state.waiting_on_update_lock = True

    def end_wait(self) -> None:
        self._state.waiting_on_update_lock = False
        self._state.waiting_since = None
        self._state.frozen_gate_sent = False

    def stamp_alert(self, *, gate: bool = False) -> None:
        """The one writer of last_alert_at: every wait alert is recorded here,
        and ``gate``: it was this wait's frozen gate (or its stand-in)."""
        self._state.last_alert_at = datetime.now(UTC).isoformat()
        self._state.frozen_gate_sent = self._state.frozen_gate_sent or gate

    def realert_due(self, every_s: float) -> bool:
        """``every_s`` since the last wait alert, or at once when this wait has
        sent none (no stamp, or one older than the wait: an unreadable lock's
        first alert is the next tick's gate). An unusable stamp counts as now
        and is repaired: the next alert comes ``every_s`` later, never a storm."""
        now = datetime.now(UTC)
        if self._state.last_alert_at is None:
            return True
        last = _usable(self._state.last_alert_at, now)
        if last is None:
            self.stamp_alert()
            return False
        start = _usable(self._state.waiting_since, now)
        return (start is not None and last < start) or (now - last).total_seconds() >= every_s

    def gate_due(self, retry_s: float) -> bool:
        """The frozen gate of this wait, sent once: due while it is not sent,
        unless the wait's last alert is within ``retry_s`` (a stand-in that
        could not be delivered retries no faster than that)."""
        if self._state.frozen_gate_sent:
            return False
        now = datetime.now(UTC)
        last = _usable(self._state.last_alert_at, now)
        start = _usable(self._state.waiting_since, now)
        return (
            last is None
            or (start is not None and last < start)
            or (now - last).total_seconds() >= retry_s
        )

    def waited_s(self) -> float | None:
        start = _usable(self._state.waiting_since, datetime.now(UTC))
        return None if start is None else (datetime.now(UTC) - start).total_seconds()

    def should_escalate(self, action: str = "") -> bool:
        """Max recovery attempts exceeded for the proposed action's OWN budget
        (#5007), the counter execute() reads: IO_TRIAGE's, or the main one."""
        if action == "IO_TRIAGE":
            max_io = getattr(self._config.recovery, "max_io_triage_attempts", 5)
            return self._state.io_triage_attempts >= max_io
        return self._state.recovery_attempts >= self._config.recovery.max_escalations
