"""TaskQualityCollector — failure rate from execution_traces in last 24h."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import SignalReading

_FAILURE_OUTCOMES = {"approach_failure", "capability_gap", "external_blocker"}


class TaskQualityCollector:
    """Queries execution_traces for recent outcome_class, returns failure rate."""

    signal_name = "task_completion_quality"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()

        cursor = await self._db.execute(
            "SELECT outcome_class FROM execution_traces WHERE created_at >= ? AND outcome_class IS NOT NULL",
            (cutoff,),
        )
        rows = await cursor.fetchall()

        if not rows:
            return self._reading(0.0)

        failures = sum(1 for r in rows if r[0] in _FAILURE_OUTCOMES)
        value = failures / len(rows)
        return self._reading(min(1.0, value))

    def _reading(self, value: float) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="execution_traces",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note="0.0=no failures in last 24h (or no tasks ran). Failure rate of recent task executions",
        )
