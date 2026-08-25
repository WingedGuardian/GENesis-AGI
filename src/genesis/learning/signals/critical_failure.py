"""CriticalFailureCollector — runs health probes and reports worst status."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from genesis.awareness.types import SignalReading
from genesis.observability.types import ProbeResult, ProbeStatus
from genesis.util import loop_health

logger = logging.getLogger(__name__)

# A DOWN whose probes ALL merely timed out is a loop-starvation artifact — not a
# real outage — only when the event loop is provably not scheduling normally. The
# primary evidence is a WEDGED loop-health sample: the on-loop lag sampler
# (hosting/standalone.py::_loop_lag_sampler) publishes every ~0.5s, so a sample
# whose age exceeds this threshold means the sampler itself couldn't run — the
# loop is stuck right now. Chosen > the 0.5s publish interval (so a normal gap
# doesn't read as wedged) and <= the 3s probe timeout (so any stall long enough
# to trip a probe also trips wedged). The fresh+lagging window is only ~0.5s, so
# WEDGED is the primary discriminator; fresh+lagging is the secondary one.
_WEDGED_AGE_S = 1.0

# Above this age, a stale sample means the publisher (the on-loop lag sampler)
# itself DIED — its docstring is explicit that "any error ends the sampler". That
# is absent LIVE evidence, not proof the loop is wedged now, so we fail-closed
# (keep 1.0) rather than suppress every subsequent DOWN forever. A real >30s loop
# wedge is itself a genuine critical failure and is caught by the external
# watchdog + the stall-stack sampler.
_STALE_CEILING_S = 30.0

# The generic explainer used on a healthy (0.0) reading and appended after the
# specific failing-probe names on a genuine DOWN/DEGRADED reading. Kept as a module
# constant so the genuine-failure path and the default path stay in sync (and so the
# #1390 invariants — mentions probes, distinguishes CLOUD LLM-provider status — hold
# on every non-suppressed reading).
_DEFAULT_NOTE = (
    "0.0=DB/Qdrant (+Ollama if enabled) health probes all healthy. "
    "0.5=a probe DEGRADED, 1.0=a probe DOWN. This tracks LOCAL "
    "infrastructure/service health (including the local Ollama if "
    "enabled), NOT cloud LLM-provider availability. A DOWN caused "
    "purely by probe timeouts while the event loop is starved is "
    "suppressed (the reading then carries a SUPPRESSED baseline_note)."
)


class CriticalFailureCollector:
    """Runs health probes: 1.0 if any DOWN, 0.5 if any DEGRADED, 0.0 if all HEALTHY.

    Loop-starvation guard: a DOWN whose probes *all* timed out, while the event
    loop is demonstrably starved (a WEDGED or fresh+lagging loop-health sample),
    is reclassified to 0.0 — a probe timing out because the loop couldn't
    schedule it is not an infrastructure outage. Fail-closed: a hard-error DOWN,
    a mixed batch with any non-timeout DOWN, a healthy loop, or absent
    loop-health evidence all keep 1.0; and any error in the suppression check
    keeps the un-suppressed value (the ``_safe_collect`` wrapper would otherwise
    turn an escaping exception into 0.0 and silently mask a real outage).
    """

    signal_name = "critical_failure"

    def __init__(
        self,
        probes: list[Callable[[], Coroutine[Any, Any, ProbeResult]]],
        *,
        wedged_age_s: float = _WEDGED_AGE_S,
        stale_ceiling_s: float = _STALE_CEILING_S,
    ) -> None:
        self._probes = probes
        self._wedged_age_s = wedged_age_s
        self._stale_ceiling_s = stale_ceiling_s

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

        if value == 1.0:
            suppressed = self._starvation_suppressed(results)
            if suppressed is not None:
                return suppressed
            down_names = ", ".join(
                r.name for r in results if r.status == ProbeStatus.DOWN
            )
            return self._reading(
                value, baseline_note=self._genuine_note("DOWN", down_names)
            )

        if value == 0.5:
            degraded_names = ", ".join(
                r.name for r in results if r.status == ProbeStatus.DEGRADED
            )
            return self._reading(
                value, baseline_note=self._genuine_note("DEGRADED", degraded_names)
            )

        return self._reading(value)

    def _genuine_note(self, status: str, names: str) -> str:
        """Name the specific probe(s) that failed, ahead of the default explainer.

        The failing-probe identity goes in ``baseline_note`` — the only field the tick
        serializer AND the reflection prompt formatter retain (``metadata`` is dropped
        by both) — so a reflection can state WHICH service failed instead of guessing
        "(DB, Qdrant, or Ollama)".
        """
        return f"Probe(s) {status}: {names}. {_DEFAULT_NOTE}"

    def _starvation_suppressed(
        self, results: list[ProbeResult]
    ) -> SignalReading | None:
        """0.0 reading if this DOWN is a loop-starvation artifact, else ``None``.

        Fully guarded: ANY failure returns ``None`` so the caller keeps
        value=1.0. Never let an exception escape to ``_safe_collect`` (which
        returns 0.0 — the suppression outcome — and would mask a real outage).
        """
        try:
            down = [r for r in results if r.status == ProbeStatus.DOWN]
            if not down or not all(r.timed_out for r in down):
                # A hard-error (non-timeout) DOWN is present -> real outage.
                return None
            sample = loop_health.read()
            if sample is None:
                # No evidence the loop is starved -> fail-closed, keep 1.0.
                return None
            age = loop_health.age_s(sample)
            # Suppress only on LIVE evidence the loop isn't scheduling right now:
            # a FRESH sample still flagged lagging, OR a sample gone stale within
            # the wedged window (the publisher missed recent beats -> loop stuck
            # now). Both are freshness-gated: a sample staler than the ceiling
            # means the publisher (the on-loop lag sampler) DIED, so its last
            # reading -- whatever its `lagging` flag says -- is absent LIVE
            # evidence, not proof the loop is wedged now. Fail-closed (keep 1.0)
            # in that case, symmetrically for lagging True and False.
            fresh_lagging = age <= self._wedged_age_s and sample.lagging
            loop_wedged = self._wedged_age_s < age <= self._stale_ceiling_s
            if not fresh_lagging and not loop_wedged:
                return None
            probes_named = ",".join(r.name for r in down)
            # Preserve a genuine DEGRADED (e.g. a 503 — the service RESPONDED, not
            # a timeout artifact): recompute the worst status over the results that
            # are NOT timeout-suppressed. Every DOWN here is timeout-suppressed, so
            # only HEALTHY/DEGRADED remain -> residual is 0.5 or 0.0. Hard-coding
            # 0.0 would mask a real degradation coincident with the stall.
            non_suppressed = [
                r
                for r in results
                if not (r.status == ProbeStatus.DOWN and r.timed_out)
            ]
            residual = (
                0.5
                if any(r.status == ProbeStatus.DEGRADED for r in non_suppressed)
                else 0.0
            )
            # Accepted residual: if a dependency is GENUINELY timing out
            # (blackholed/overloaded) at the same instant the loop is wedged, this
            # suppresses it for this tick. Unresolvable from a single reading
            # (loop_health proves the loop is starved, never that the dependency is
            # reachable) and self-healing — the next tick with a healthy loop fires
            # 1.0. critical_failure is a reflection signal; hard failures are the
            # status.json watchdog's domain.
            logger.warning(
                "critical_failure suppressed to %.1f: all DOWN probes (%s) timed "
                "out under event-loop starvation (drift=%.0fms, sample_age=%.2fs) "
                "— infra health indeterminate (loop couldn't schedule the probe), "
                "re-checked next tick",
                residual,
                probes_named,
                sample.drift_ms,
                age,
            )
            # The suppression explanation goes in baseline_note — the field the
            # tick serializer + reflection prompt formatter actually retain
            # (metadata is dropped by both) — so a suppressed reading is never
            # mistaken for a genuinely-healthy 0.0.
            remainder = (
                "a genuinely-degraded probe remains" if residual else "no other fault"
            )
            return self._reading(
                residual,
                baseline_note=(
                    f"SUPPRESSED under event-loop starvation: probe(s) "
                    f"{probes_named} timed out because the loop couldn't schedule "
                    f"them (drift {sample.drift_ms:.0f}ms), so their health is "
                    f"INDETERMINATE this tick and is not counted as a critical "
                    f"failure (re-checked next tick). Value {residual} ({remainder})."
                ),
                metadata={
                    "starvation_suppressed": True,
                    "suppressed_probes": probes_named,
                    "residual_value": residual,
                    "loop_drift_ms": round(sample.drift_ms, 1),
                    "loop_sample_age_s": round(age, 2),
                    "loop_executor": sample.executor,
                },
            )
        except Exception:
            logger.warning(
                "critical_failure starvation-suppression check failed; keeping "
                "value=1.0",
                exc_info=True,
            )
            return None

    def _reading(
        self,
        value: float,
        *,
        metadata: dict | None = None,
        baseline_note: str | None = None,
    ) -> SignalReading:
        return SignalReading(
            name=self.signal_name,
            value=value,
            source="health_probes",
            collected_at=datetime.now(UTC).isoformat(),
            baseline_note=baseline_note or _DEFAULT_NOTE,
            metadata=metadata,
        )
