"""Obstacle escalation — retry limits, escalation reports, never-silently-fail.

Key invariant: Genesis NEVER silently gives up on a task. If it can't solve
a problem after exhausting alternatives, it surfaces a structured escalation
report to the user with full context on what was tried and what help is needed.
"""

from __future__ import annotations

import logging

from genesis.autonomy.types import EscalationReport

logger = logging.getLogger(__name__)


class EscalationManager:
    """Manages retry limits and escalation reporting for autonomous tasks."""

    # Retry limits by autonomy level bracket.
    _RETRY_LIMITS: dict[tuple[int, int], int] = {
        (1, 2): 2,  # L1-L2: 2 alternative approaches before escalating
        (3, 4): 4,  # L3-L4: 4 alternatives before escalating
    }

    def max_retries(self, autonomy_level: int) -> int:
        """Return the maximum retry count for the given autonomy level."""
        for (lo, hi), limit in self._RETRY_LIMITS.items():
            if lo <= autonomy_level <= hi:
                return limit
        # Default conservative: escalate quickly for unknown levels.
        logger.warning(
            "No retry limit configured for autonomy level %d, defaulting to 2",
            autonomy_level,
        )
        return 2

    def should_escalate(self, autonomy_level: int, attempts: int) -> bool:
        """Return True if attempts have reached or exceeded the retry limit."""
        return attempts >= self.max_retries(autonomy_level)

    def build_escalation_report(
        self,
        *,
        task_id: str,
        attempts: list[str],
        final_blocker: str,
        alternatives_considered: list[str],
        help_needed: str,
    ) -> EscalationReport:
        """Create a structured escalation report.

        All fields are required via keyword-only args to prevent accidental
        omission — the whole point is to never silently drop context.
        """
        if not task_id:
            raise ValueError("task_id is required for escalation reports")
        if not final_blocker:
            raise ValueError("final_blocker is required — never escalate without explaining why")
        if not help_needed:
            raise ValueError("help_needed is required — always say what would unblock you")

        report = EscalationReport(
            task_id=task_id,
            attempts=list(attempts),
            final_blocker=final_blocker,
            alternatives_considered=list(alternatives_considered),
            help_needed=help_needed,
        )

        logger.error(
            "Escalating task %s after %d attempts: %s",
            task_id,
            len(attempts),
            final_blocker,
        )

        return report

    def format_escalation_message(self, report: EscalationReport) -> str:
        """Format an escalation report for human-readable delivery."""
        lines: list[str] = []

        lines.append(f"\U0001f6a8 Escalation: Task {report.task_id}")
        lines.append("")

        lines.append("What I tried:")
        for i, attempt in enumerate(report.attempts, 1):
            lines.append(f"{i}. {attempt}")
        lines.append("")

        lines.append("What's blocking me:")
        lines.append(report.final_blocker)
        lines.append("")

        lines.append("Alternatives I considered:")
        for alt in report.alternatives_considered:
            lines.append(f"- {alt}")
        lines.append("")

        lines.append("What I need to unblock:")
        lines.append(report.help_needed)

        return "\n".join(lines)
