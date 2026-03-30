"""CriticalFailureCollector — runs health probes and reports worst status."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from genesis.awareness.types import SignalReading
from genesis.observability.types import ProbeResult, ProbeStatus


class CriticalFailureCollector:
    """Runs health probes: 1.0 if any DOWN, 0.5 if any DEGRADED, 0.0 if all HEALTHY."""

    signal_name = "critical_failure"

    def __init__(self, probes: list[Callable[[], Coroutine[Any, Any, ProbeResult]]]) -> None:
        self._probes = probes

    async def collect(self) -> SignalReading:
        if not self._probes:
            return self._reading(0.0)

        results: list[ProbeResult] = await asyncio.gather(
            *(probe() for probe in self._probes)
        )

        if any(r.status == ProbeStatus.DOWN for r in results):
            value = 1.0
        elif any(r.status == ProbeStatus.DEGRADED for r in results):
            value = 0.5
        else:
            value = 0.0

        return self._reading(value)

    def _reading(self, value: float) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="health_probes",
            collected_at=datetime.now(UTC).isoformat(),
        )
