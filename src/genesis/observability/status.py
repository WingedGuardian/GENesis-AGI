"""SystemStatusAggregator — on-demand system health snapshot."""

from __future__ import annotations

from datetime import UTC, datetime

from genesis.observability.types import ProbeResult, ProbeStatus, SystemSnapshot


class SystemStatusAggregator:
    """Collects probe results into a SystemSnapshot on demand.

    Usage::

        agg = SystemStatusAggregator()
        agg.register_probe(probe_db, db)       # registers (coro_fn, *args)
        agg.register_probe(probe_qdrant)
        snap = await agg.snapshot()
    """

    def __init__(self, *, clock=None):
        self._probes: list[tuple] = []  # (async_fn, args, kwargs)
        self._clock = clock or (lambda: datetime.now(UTC))

    def register_probe(self, probe_fn, *args, **kwargs) -> None:
        """Register a probe function to be called during snapshot()."""
        self._probes.append((probe_fn, args, kwargs))

    async def snapshot(self) -> SystemSnapshot:
        """Run all registered probes and build a SystemSnapshot."""
        results: list[ProbeResult] = []
        for probe_fn, args, kwargs in self._probes:
            try:
                result = await probe_fn(*args, **kwargs)
                results.append(result)
            except Exception as exc:
                results.append(ProbeResult(
                    name=getattr(probe_fn, "__name__", "unknown"),
                    status=ProbeStatus.DOWN,
                    latency_ms=0.0,
                    message=f"Probe raised: {exc}",
                    checked_at=self._clock().isoformat(),
                ))

        overall = all(r.status == ProbeStatus.HEALTHY for r in results)
        return SystemSnapshot(
            timestamp=self._clock().isoformat(),
            probes=results,
            overall_healthy=overall,
        )
