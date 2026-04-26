# GROUNDWORK(cross-vendor-review): V4 will activate cross-vendor review for costly-reversible auto-approval
"""Disagreement detection and rate tracking for autonomy decisions.

In V3, the DisagreementGate is structural only — review() is a no-op that
returns "no disagreement". V4 will wire in cross-vendor review so that
costly-reversible actions get a second opinion before auto-approval.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)

_ROLLING_WINDOW = 100


@dataclass
class DisagreementResult:
    """Result of a disagreement gate review."""

    agreed: bool
    primary_assessment: str
    secondary_assessment: str | None = None
    recommendation: str = ""


class DisagreementGate:
    """Cross-vendor disagreement detection and calibration tracking.

    # GROUNDWORK(cross-vendor-review): V4 will replace the no-op review()
    # with actual cross-vendor calls (e.g. Gemini vs Claude) for actions
    # classified as costly-reversible or irreversible.
    """

    def __init__(self, *, event_bus: object | None = None) -> None:
        self._event_bus = event_bus
        self._disagreements: dict[str, list[bool]] = defaultdict(list)

    async def review(
        self,
        *,
        action_description: str,
        primary_assessment: str,
        action_class: str,
    ) -> DisagreementResult:
        """Review an action for cross-vendor disagreement.

        # GROUNDWORK(cross-vendor-review): V3 no-op — always returns agreed.
        # V4 will dispatch to a secondary vendor and compare assessments.
        """
        logger.debug(
            "Disagreement gate review (V3 no-op): action_class=%s description=%s",
            action_class,
            action_description[:80],
        )
        return DisagreementResult(
            agreed=True,
            primary_assessment=primary_assessment,
            secondary_assessment=None,
            recommendation="",
        )

    def record_disagreement(
        self,
        *,
        domain: str,
        primary: str,
        secondary: str,
    ) -> None:
        """Record a disagreement occurrence for calibration tracking."""
        entries = self._disagreements[domain]
        entries.append(True)
        if len(entries) > _ROLLING_WINDOW:
            self._disagreements[domain] = entries[-_ROLLING_WINDOW:]

        logger.warning(
            "Disagreement recorded in domain=%s: primary=%s secondary=%s",
            domain,
            primary[:120],
            secondary[:120],
        )

        if self._event_bus is not None:
            try:
                import contextlib

                from genesis.util.tasks import tracked_task

                coro = self._event_bus.emit(
                    Subsystem.AUTONOMY,
                    Severity.WARNING,
                    "autonomy.disagreement",
                    f"Disagreement in {domain}",
                    domain=domain,
                    primary=primary,
                    secondary=secondary,
                )
                with contextlib.suppress(RuntimeError):
                    tracked_task(
                        coro, name="autonomy.disagreement-emit",
                        subsystem=Subsystem.AUTONOMY, logger=logger,
                    )
            except Exception:
                logger.error(
                    "Failed to emit disagreement event for domain=%s",
                    domain,
                    exc_info=True,
                )

    def disagreement_rate(self, domain: str) -> float:
        """Return disagreement rate for a domain (0.0–1.0)."""
        entries = self._disagreements.get(domain)
        if not entries:
            return 0.0
        return sum(entries) / len(entries)

    def is_calibration_concern(
        self,
        domain: str,
        *,
        threshold: float = 0.3,
    ) -> bool:
        """True if disagreement rate exceeds threshold (default 30%)."""
        return self.disagreement_rate(domain) > threshold
