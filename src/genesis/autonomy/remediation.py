"""Self-healing remediation registry -- maps health probe results to corrective actions.

Design:
  - RemediationAction describes WHAT to do when a probe fails
  - RemediationRegistry evaluates probe results and runs matching actions
  - Governance levels control automation vs human involvement:
      L2 = auto-run (known safe, reversible)
      L3 = propose via outreach (needs user confirmation)
      L4 = alert only (informational)
  - Cooldown + max-attempts prevent runaway remediation loops
  - All command runs through asyncio.create_subprocess_exec (never os.kill)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from genesis.observability.types import ProbeStatus
from genesis.util.systemd import systemctl_env

logger = logging.getLogger(__name__)

# Outreach callback: async (severity: str, title: str, body: str) -> None
OutreachCallback = Callable[[str, str, str], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class RemediationAction:
    """A single remediation that can fire when a health probe fails."""

    name: str
    probe_name: str          # Which health probe triggers this
    condition: str           # Human-readable trigger condition
    command: list[str]       # What to run (e.g. ["systemctl", "restart", "qdrant"])
    governance_level: int    # L2=auto, L3=confirm, L4=alert-only
    reversible: bool = True
    cooldown_s: int = 300    # Min seconds between runs
    max_attempts: int = 3    # Max consecutive attempts before giving up


@dataclass
class RemediationOutcome:
    """Result of evaluating/running a single remediation action."""

    action: RemediationAction
    triggered: bool          # Did the probe indicate failure?
    executed: bool           # Did we actually run the command?
    success: bool | None     # None if not run
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class RemediationRegistry:
    """Maps health probe results to remediation actions.

    Thread-safe via asyncio.Lock -- only one remediation runs at a time
    to prevent cascading restarts.
    """

    def __init__(self, *, outreach_fn: OutreachCallback | None = None) -> None:
        self._actions: list[RemediationAction] = []
        self._last_run: dict[str, float] = {}  # name -> monotonic timestamp
        self._consecutive_failures: dict[str, int] = {}  # name -> count
        self._lock = asyncio.Lock()
        self._outreach_fn = outreach_fn
        self._escalation_callback = None

    def set_escalation_callback(self, fn) -> None:
        """Set callback for when remediation is exhausted.

        Callback signature: async (trigger_source, tier, reason, context) -> Any
        Used by the Sentinel to dispatch CC diagnosis when reflexes fail.
        """
        self._escalation_callback = fn

    @property
    def actions(self) -> list[RemediationAction]:
        """Read-only access to registered actions (for testing/inspection)."""
        return list(self._actions)

    def register(self, action: RemediationAction) -> None:
        """Register a remediation action. Idempotent by name."""
        if any(a.name == action.name for a in self._actions):
            logger.debug("Remediation %r already registered -- skipping", action.name)
            return
        self._actions.append(action)
        logger.info(
            "Registered remediation: %s (probe=%s, L%d)",
            action.name, action.probe_name, action.governance_level,
        )

    async def check_and_remediate(
        self,
        probe_results: dict[str, Any],
    ) -> list[RemediationOutcome]:
        """Check probe results against all registered actions and remediate.

        Args:
            probe_results: dict mapping probe name to ProbeResult (or dict
                with at least a "status" key).

        Returns:
            List of outcomes for every action whose probe was present.
        """
        outcomes: list[RemediationOutcome] = []
        async with self._lock:
            for action in self._actions:
                probe = probe_results.get(action.probe_name)
                if probe is None:
                    continue
                outcome = await self._evaluate(action, probe)
                outcomes.append(outcome)
        return outcomes

    def reset_failures(self, name: str) -> None:
        """Reset consecutive failure count for a named remediation."""
        self._consecutive_failures.pop(name, None)

    async def _evaluate(
        self,
        action: RemediationAction,
        probe: Any,
    ) -> RemediationOutcome:
        """Evaluate a single action against its probe result."""
        # Determine probe status
        status = _extract_status(probe)
        if status == ProbeStatus.HEALTHY:
            # Probe healthy -- reset failure counter
            self._consecutive_failures.pop(action.name, None)
            return RemediationOutcome(
                action=action,
                triggered=False,
                executed=False,
                success=None,
                message="Probe healthy",
            )

        # Probe indicates failure -- action is triggered
        # Check max attempts
        failures = self._consecutive_failures.get(action.name, 0)
        if failures >= action.max_attempts:
            msg = (
                f"Max attempts ({action.max_attempts}) reached for "
                f"{action.name} -- manual intervention needed"
            )
            logger.error(msg)
            if self._outreach_fn:
                try:
                    await self._outreach_fn(
                        "critical",
                        f"Remediation exhausted: {action.name}",
                        msg,
                    )
                except Exception:
                    logger.error(
                        "Failed to send outreach for exhausted remediation",
                        exc_info=True,
                    )
            if self._escalation_callback:
                try:
                    await self._escalation_callback(
                        trigger_source="remediation_exhausted",
                        tier=3,
                        reason=msg,
                        context={"action_name": action.name, "probe_name": action.probe_name},
                    )
                except Exception:
                    logger.error(
                        "Failed to escalate exhausted remediation to Sentinel",
                        exc_info=True,
                    )
            return RemediationOutcome(
                action=action,
                triggered=True,
                executed=False,
                success=None,
                message=msg,
            )

        # Check cooldown
        last_run_at = self._last_run.get(action.name)
        if last_run_at is not None:
            elapsed = time.monotonic() - last_run_at
            if elapsed < action.cooldown_s:
                return RemediationOutcome(
                    action=action,
                    triggered=True,
                    executed=False,
                    success=None,
                    message=f"In cooldown -- {action.cooldown_s - elapsed:.0f}s remaining",
                )

        # Governance routing
        if action.governance_level >= 4:
            # L4: alert only
            msg = (
                f"Alert: {action.name} triggered "
                f"(probe {action.probe_name} is {status})"
            )
            logger.warning(msg)
            if self._outreach_fn:
                try:
                    await self._outreach_fn(
                        "warning",
                        f"Health alert: {action.name}",
                        msg,
                    )
                except Exception:
                    logger.error("Failed to send alert outreach", exc_info=True)
            self._last_run[action.name] = time.monotonic()
            return RemediationOutcome(
                action=action,
                triggered=True,
                executed=False,
                success=None,
                message=msg,
            )

        if action.governance_level == 3:
            # L3: propose via outreach, don't auto-run
            msg = f"Proposing remediation: {action.name} -- {action.condition}"
            logger.info(msg)
            if self._outreach_fn:
                try:
                    await self._outreach_fn(
                        "error",
                        f"Remediation proposed: {action.name}",
                        f"{action.condition}\nCommand: {' '.join(action.command)}",
                    )
                except Exception:
                    logger.error("Failed to send proposal outreach", exc_info=True)
            self._last_run[action.name] = time.monotonic()
            return RemediationOutcome(
                action=action,
                triggered=True,
                executed=False,
                success=None,
                message=msg,
            )

        # L2: auto-run
        logger.info(
            "Running remediation: %s -- %s",
            action.name, " ".join(action.command),
        )
        success = await self._run_command(action)
        self._last_run[action.name] = time.monotonic()
        if success:
            self._consecutive_failures.pop(action.name, None)
            return RemediationOutcome(
                action=action,
                triggered=True,
                executed=True,
                success=True,
                message=f"Remediation {action.name} succeeded",
            )
        else:
            self._consecutive_failures[action.name] = failures + 1
            return RemediationOutcome(
                action=action,
                triggered=True,
                executed=True,
                success=False,
                message=(
                    f"Remediation {action.name} failed "
                    f"(attempt {failures + 1}/{action.max_attempts})"
                ),
            )

    async def _run_command(self, action: RemediationAction) -> bool:
        """Run a remediation command. Returns True on success."""
        if not action.command:
            logger.error("Remediation %s has empty command -- skipping", action.name)
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                *action.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=systemctl_env(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30,
            )
            if proc.returncode == 0:
                logger.info("Remediation %s completed (rc=0)", action.name)
                return True
            logger.error(
                "Remediation %s failed (rc=%d): %s",
                action.name,
                proc.returncode,
                stderr.decode(errors="replace").strip()[:500],
            )
            return False
        except TimeoutError:
            logger.error(
                "Remediation %s timed out after 30s", action.name, exc_info=True,
            )
            return False
        except FileNotFoundError:
            logger.error(
                "Remediation %s command not found: %s",
                action.name,
                action.command[0],
                exc_info=True,
            )
            return False
        except Exception:
            logger.error(
                "Remediation %s failed unexpectedly", action.name, exc_info=True,
            )
            return False


def _extract_status(probe: Any) -> ProbeStatus:
    """Extract ProbeStatus from a ProbeResult or dict.

    Unknown/malformed inputs return DOWN (fail-safe) — a probe that
    cannot be evaluated should not suppress remediation.
    """
    if hasattr(probe, "status"):
        status = probe.status
    elif isinstance(probe, dict):
        status = probe.get("status", ProbeStatus.DOWN)
    else:
        return ProbeStatus.DOWN

    if isinstance(status, ProbeStatus):
        return status
    try:
        return ProbeStatus(str(status))
    except ValueError:
        return ProbeStatus.DOWN


# ---------------------------------------------------------------------------
# Default remediation actions
# ---------------------------------------------------------------------------

DEFAULT_REMEDIATIONS: list[RemediationAction] = [
    RemediationAction(
        name="qdrant_restart",
        probe_name="qdrant",
        condition="Qdrant health probe returns DOWN",
        command=["systemctl", "restart", "qdrant"],
        governance_level=2,
        reversible=True,
        cooldown_s=300,
        max_attempts=3,
    ),
    RemediationAction(
        name="tmp_cleanup",
        probe_name="tmp_usage",
        condition="/tmp usage exceeds 80%",
        command=[
            "bash", "-c",
            'find /tmp -maxdepth 1 -type f \\( -name "claude-*" -o -name ".claude-*" \\) -mmin +60 -delete',
        ],
        governance_level=2,
        reversible=True,
        cooldown_s=600,
        max_attempts=5,
    ),
    RemediationAction(
        name="awareness_restart",
        probe_name="awareness_tick",
        condition="Awareness loop heartbeat stale >15min",
        command=["systemctl", "--user", "restart", "genesis-bridge.service"],
        governance_level=2,
        reversible=True,
        cooldown_s=300,
        max_attempts=3,
    ),
    RemediationAction(
        name="ollama_alert",
        probe_name="ollama",
        condition="Ollama service unreachable",
        command=[],  # Alert-only, no command
        governance_level=4,
        reversible=True,
        cooldown_s=86400,
        max_attempts=1,
    ),
    RemediationAction(
        name="disk_cleanup",
        probe_name="disk",
        condition="Root filesystem usage exceeds 90%",
        command=["sudo", "journalctl", "--vacuum-size=100M"],
        governance_level=3,
        reversible=True,
        cooldown_s=86400,
        max_attempts=2,
    ),
]


def register_defaults(registry: RemediationRegistry) -> None:
    """Register all default remediation actions."""
    for action in DEFAULT_REMEDIATIONS:
        registry.register(action)
