"""Inline failure detector — real-time procedure confidence updates.

Two detection paths with near-zero false positive design:
1. CC exceptions (from conversation.py error handlers) — unambiguous failures
2. User correction patterns (regex on user input) — low false positive risk

Does NOT scan CC output text. "I fixed the permission_denied error" never
triggers because successful output is never scanned.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# ── User correction patterns ─────────────────────────────────────────────────
# Compiled once at module load. Scanned against user input only.

_USER_CORRECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("explicit_failure", re.compile(
        r"\b(that didn'?t work|still broken|wrong answer|that'?s wrong|incorrect)\b",
        re.IGNORECASE,
    )),
    ("retry_request", re.compile(
        r"\b(try again|try differently|try another|retry|redo)\b",
        re.IGNORECASE,
    )),
    ("tool_direction", re.compile(
        r"\b(don'?t use|stop using|use .+ instead)\b",
        re.IGNORECASE,
    )),
]

# TTL dedup: same failure_type within this window → skip
_DEDUP_WINDOW_S = 300  # 5 minutes


class FailureDetector:
    """Detects failures and updates procedure confidence in real-time."""

    def __init__(self) -> None:
        self._recent: dict[str, float] = {}  # failure_type → timestamp

    def scan_user_input(self, text: str) -> str | None:
        """Scan user message for correction patterns. Returns failure type or None."""
        for failure_type, pattern in _USER_CORRECTION_PATTERNS:
            if pattern.search(text):
                if self._is_dedup(failure_type):
                    return None
                self._recent[failure_type] = time.monotonic()
                return failure_type
        return None

    async def record_failure(
        self,
        db: aiosqlite.Connection,
        failure_type: str,
        context_tags: list[str] | None = None,
    ) -> bool:
        """Record a failure against the best-matching procedure.

        Returns True if a procedure was found and updated.
        """
        from genesis.learning.procedural.matcher import find_best_match
        from genesis.learning.procedural.operations import record_failure as proc_fail

        tags = context_tags or []
        match = await find_best_match(db, failure_type, tags)
        if match is None:
            return False

        await proc_fail(db, match.procedure_id, condition=failure_type)
        logger.info(
            "Failure detector: recorded %s against procedure %s (conf=%.2f)",
            failure_type, match.procedure_id[:8], match.confidence,
        )
        return True

    async def record_cc_error(
        self,
        db: aiosqlite.Connection,
        error_type: str,
        context_tags: list[str] | None = None,
    ) -> bool:
        """Record a CC exception as a procedure failure.

        Called from conversation.py error handlers. Zero false positive risk
        because these are real caught exceptions.
        """
        if self._is_dedup(error_type):
            return False
        self._recent[error_type] = time.monotonic()
        return await self.record_failure(db, error_type, context_tags)

    def _is_dedup(self, failure_type: str) -> bool:
        """Check if this failure type was seen recently."""
        now = time.monotonic()
        # Evict stale entries
        self._recent = {
            k: v for k, v in self._recent.items()
            if now - v < _DEDUP_WINDOW_S
        }
        last = self._recent.get(failure_type)
        return last is not None and (now - last) < _DEDUP_WINDOW_S
