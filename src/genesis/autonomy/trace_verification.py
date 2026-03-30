"""Decision trace verification — programmatic checks that stated reasons match actual data."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from genesis.observability.types import GenesisEvent, Severity, Subsystem

logger = logging.getLogger(__name__)

_ROLLING_WINDOW = 100


@dataclass
class TraceResult:
    """Result of verifying a single decision trace."""

    passed: bool
    decision_type: str
    stated_reason: str
    mismatch_detail: str = ""


def _verify_routing(payload: dict) -> TraceResult:
    """Built-in verifier for routing decisions.

    Checks whether ``actual_data["token_count"]`` is consistent with claims
    in ``stated_reason`` (e.g. "token count > 500").
    """
    stated = payload["stated_reason"]
    actual = payload["actual_data"]
    token_count = actual.get("token_count")

    if token_count is None:
        return TraceResult(
            passed=True,
            decision_type="routing",
            stated_reason=stated,
            mismatch_detail="no token_count in actual_data, cannot verify",
        )

    # Look for patterns like "> 500", ">= 500", "< 500", "<= 500"
    match = re.search(r"token\s*count\s*(>=?|<=?)\s*(\d+)", stated, re.IGNORECASE)
    if not match:
        return TraceResult(
            passed=True,
            decision_type="routing",
            stated_reason=stated,
            mismatch_detail="no token count claim found in stated reason",
        )

    op_str, threshold_str = match.group(1), match.group(2)
    threshold = int(threshold_str)
    ops = {
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
    }
    check = ops[op_str]
    passed = check(token_count, threshold)

    detail = "" if passed else (
        f"stated 'token count {op_str} {threshold}' "
        f"but actual token_count={token_count}"
    )
    return TraceResult(
        passed=passed,
        decision_type="routing",
        stated_reason=stated,
        mismatch_detail=detail,
    )


def _verify_triage(payload: dict) -> TraceResult:
    """Built-in verifier for triage decisions.

    Checks whether ``actual_data["priority"]`` matches the priority level
    mentioned in ``stated_reason``.
    """
    stated = payload["stated_reason"]
    actual = payload["actual_data"]
    actual_priority = str(actual.get("priority", "")).strip().lower()

    if not actual_priority:
        return TraceResult(
            passed=True,
            decision_type="triage",
            stated_reason=stated,
            mismatch_detail="no priority in actual_data, cannot verify",
        )

    # Check if the stated reason contains the actual priority string
    passed = actual_priority in stated.lower()
    detail = "" if passed else (
        f"stated reason does not mention actual priority '{actual_priority}'"
    )
    return TraceResult(
        passed=passed,
        decision_type="triage",
        stated_reason=stated,
        mismatch_detail=detail,
    )


class TraceVerifier:
    """Programmatically verifies that stated reasons match actual data.

    Maintains per-decision-type rolling windows of match/mismatch results
    and exposes a confabulation-concern flag when mismatch rates exceed
    a configurable threshold.
    """

    def __init__(self, *, event_bus: object | None = None) -> None:
        self._event_bus = event_bus
        self._verifiers: dict[str, Callable[[dict], TraceResult]] = {}
        self._results: dict[str, list[bool]] = {}

        # Register built-in verifiers
        self.register("routing", _verify_routing)
        self.register("triage", _verify_triage)

    def register(
        self, decision_type: str, verifier: Callable[[dict], TraceResult]
    ) -> None:
        """Register a verifier function for a decision type."""
        self._verifiers[decision_type] = verifier

    def verify(
        self,
        *,
        decision_type: str,
        stated_reason: str,
        actual_data: dict,
    ) -> TraceResult:
        """Look up verifier for *decision_type* and run it.

        If no verifier is registered, returns a passing result with a note.
        On mismatch, emits an observability event and records the result.
        """
        verifier = self._verifiers.get(decision_type)
        if verifier is None:
            return TraceResult(
                passed=True,
                decision_type=decision_type,
                stated_reason=stated_reason,
                mismatch_detail="no verifier registered",
            )

        payload = {"stated_reason": stated_reason, "actual_data": actual_data}
        try:
            result = verifier(payload)
        except Exception:
            logger.error(
                "Trace verifier for %s raised an exception",
                decision_type,
                exc_info=True,
            )
            result = TraceResult(
                passed=False,
                decision_type=decision_type,
                stated_reason=stated_reason,
                mismatch_detail="verifier raised an exception",
            )

        # Record in rolling window
        window = self._results.setdefault(decision_type, [])
        window.append(result.passed)
        if len(window) > _ROLLING_WINDOW:
            window[:] = window[-_ROLLING_WINDOW:]

        # Emit event on mismatch
        if not result.passed:
            logger.warning(
                "Trace mismatch for %s: %s",
                decision_type,
                result.mismatch_detail,
            )
            self._emit_mismatch_event(result)

        return result

    def mismatch_rate(self, decision_type: str) -> float:
        """Return mismatch rate for *decision_type* (0.0–1.0)."""
        window = self._results.get(decision_type)
        if not window:
            return 0.0
        return sum(1 for passed in window if not passed) / len(window)

    def is_confabulation_concern(
        self, decision_type: str, *, threshold: float = 0.2
    ) -> bool:
        """True if mismatch rate exceeds *threshold* (default 20%)."""
        return self.mismatch_rate(decision_type) > threshold

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_mismatch_event(self, result: TraceResult) -> None:
        """Emit a calibration.trace_mismatch event if an event bus is available."""
        if self._event_bus is None:
            return

        # The event bus may construct the event internally, so pass components
        emit = getattr(self._event_bus, "emit", None)
        if not callable(emit):
            return
        try:
            emit(
                GenesisEvent(
                    subsystem=Subsystem.AUTONOMY,
                    severity=Severity.WARNING,
                    event_type="calibration.trace_mismatch",
                    message=(
                        f"Trace mismatch for {result.decision_type}: "
                        f"{result.mismatch_detail}"
                    ),
                    timestamp=datetime.now(UTC).isoformat(),
                    details={
                        "decision_type": result.decision_type,
                        "stated_reason": result.stated_reason,
                        "mismatch_detail": result.mismatch_detail,
                    },
                )
            )
        except Exception:
            logger.debug("Failed to emit trace mismatch event", exc_info=True)
