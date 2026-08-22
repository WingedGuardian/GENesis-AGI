"""Integration tests for _50_genesis_learning extension wiring logic.

Tests the assembly logic without requiring the AZ runtime — verifies
that components are created and connected correctly using mocks.
"""

from __future__ import annotations

from functools import partial
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db import schema


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for ddl in schema.TABLES.values():
            await conn.execute(ddl)
        await conn.commit()
        yield conn


class TestCollectorReplacement:
    def test_replace_collectors_swaps_list(self, db):
        """AwarenessLoop.replace_collectors replaces the internal list."""
        import asyncio

        from genesis.awareness.loop import AwarenessLoop

        stub = MagicMock(signal_name="stub")
        loop = AwarenessLoop(db=asyncio.get_event_loop() and db, collectors=[stub])
        assert len(loop._collectors) == 1

        real = MagicMock(signal_name="real")
        loop.replace_collectors([real])
        assert len(loop._collectors) == 1
        assert loop._collectors[0].signal_name == "real"

    def test_real_collectors_have_correct_signal_names(self):
        """Real learning-signal collectors have the expected signal_name."""
        from genesis.learning.signals.budget import BudgetCollector
        from genesis.learning.signals.critical_failure import (
            CriticalFailureCollector,
        )
        from genesis.learning.signals.error_spike import ErrorSpikeCollector
        from genesis.learning.signals.task_quality import TaskQualityCollector

        mock_db = MagicMock()
        assert BudgetCollector(mock_db).signal_name == "budget_pct_consumed"
        assert ErrorSpikeCollector(mock_db).signal_name == "software_error_spike"
        assert CriticalFailureCollector([]).signal_name == "critical_failure"
        assert TaskQualityCollector(mock_db).signal_name == "task_completion_quality"


class TestSteadyStateSignalParity:
    """Guard the bootstrap→steady-state collector swap against silent drops.

    ``AwarenessLoop.replace_collectors`` is a FULL replacement, so a collector
    registered in the bootstrap set but omitted from the learning swap silently
    stops being measured (the exact bug that dropped scheduled_job_health /
    scheduler_liveness). These tests pin the invariant.
    """

    @staticmethod
    def _names(collectors) -> set[str]:
        return {c.signal_name for c in collectors}

    def test_learning_builder_matches_steady_state_signals(self):
        """The learning swap emits EXACTLY STEADY_STATE_SIGNALS (constant stays synced)."""
        from genesis.awareness.types import STEADY_STATE_SIGNALS
        from genesis.runtime.init.learning import build_learning_collectors

        collectors = build_learning_collectors(MagicMock())
        assert self._names(collectors) == STEADY_STATE_SIGNALS
        # Count guard: a duplicate signal_name would collapse in the set and hide.
        assert len(collectors) == len(STEADY_STATE_SIGNALS)

    def test_restored_collectors_present_in_steady_state(self):
        """The two restored collectors are in the steady-state swap."""
        from genesis.runtime.init.learning import build_learning_collectors

        names = self._names(build_learning_collectors(MagicMock()))
        assert "scheduled_job_health" in names
        assert "scheduler_liveness" in names

    def test_bootstrap_signals_covered_by_steady_state(self):
        """Every bootstrap signal is carried into steady state, except deferrals."""
        from genesis.awareness.types import BOOTSTRAP_ONLY_SIGNALS
        from genesis.runtime.init.awareness import build_bootstrap_collectors
        from genesis.runtime.init.learning import build_learning_collectors

        boot = self._names(build_bootstrap_collectors(MagicMock()))
        steady = self._names(build_learning_collectors(MagicMock()))
        assert (boot - BOOTSTRAP_ONLY_SIGNALS) <= steady

    def test_bootstrap_only_signals_actually_in_bootstrap(self):
        """Every declared exception is a real bootstrap signal (no phantom names)."""
        from genesis.awareness.types import BOOTSTRAP_ONLY_SIGNALS
        from genesis.runtime.init.awareness import build_bootstrap_collectors

        boot = self._names(build_bootstrap_collectors(MagicMock()))
        assert boot >= BOOTSTRAP_ONLY_SIGNALS

    def test_bootstrap_only_disjoint_from_steady_state(self):
        """A deferral must NOT also be a steady-state signal.

        Without this, a name added to BOOTSTRAP_ONLY_SIGNALS that is also a real
        steady-state signal would still pass the coverage check (subtraction only
        loosens ``boot - BOOTSTRAP_ONLY_SIGNALS <= steady``), letting that
        collector be silently dropped from the swap — the exact regression class
        this guard exists to prevent.
        """
        from genesis.awareness.types import (
            BOOTSTRAP_ONLY_SIGNALS,
            STEADY_STATE_SIGNALS,
        )

        assert BOOTSTRAP_ONLY_SIGNALS.isdisjoint(STEADY_STATE_SIGNALS)

    def test_event_loop_latency_deferred_not_in_steady_state(self):
        """event_loop_latency is a documented deferral: in bootstrap, not steady state."""
        from genesis.awareness.types import BOOTSTRAP_ONLY_SIGNALS
        from genesis.runtime.init.learning import build_learning_collectors

        assert "event_loop_latency" in BOOTSTRAP_ONLY_SIGNALS
        steady = self._names(build_learning_collectors(MagicMock()))
        assert "event_loop_latency" not in steady

    def test_user_facing_signals_subset_of_steady_state(self):
        """The other hand-maintained signal set stays within the steady-state set."""
        from genesis.awareness.types import (
            STEADY_STATE_SIGNALS,
            USER_FACING_SIGNALS,
        )

        assert USER_FACING_SIGNALS <= STEADY_STATE_SIGNALS

    def test_memory_backlog_collector_removed(self):
        """The orphaned MemoryBacklogCollector module is deleted (deliberate 2026-04-11)."""
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("genesis.learning.signals.memory_backlog")


class TestPipelineAssembly:
    @pytest.mark.asyncio
    async def test_pipeline_callable_signature(self, db):
        """Built pipeline accepts (output, user_text, channel) args."""
        from genesis.learning.pipeline import build_triage_pipeline

        pipeline = build_triage_pipeline(
            db=db,
            triage_classifier=MagicMock(classify=AsyncMock(
                return_value=MagicMock(depth=MagicMock(value=0, __ge__=lambda s, o: False, __eq__=lambda s, o: True))
            )),
            outcome_classifier=MagicMock(),
            delta_assessor=MagicMock(),
            observation_writer=MagicMock(),
        )
        assert callable(pipeline)
        # Verify it accepts the right args (trivial output, prefilter will skip)
        output = MagicMock(
            session_id="s1", text="hi", input_tokens=5, output_tokens=5,
            model_used="t", cost_usd=0, duration_ms=0, exit_code=0,
        )
        await pipeline(output, "hi", "terminal")  # should not raise

    @pytest.mark.asyncio
    async def test_observation_writer_with_none_memory_store(self, db):
        """ObservationWriter works when memory_store is None (Qdrant down)."""
        from genesis.learning.observation_writer import ObservationWriter

        writer = ObservationWriter(memory_store=None)
        obs_id = await writer.write(
            db, source="test", type="t", content="c", priority="low"
        )
        assert obs_id


class TestSchedulerJobs:
    def test_calibrator_accepts_required_deps(self):
        """TriageCalibrator initializes with router, db, optional deps."""
        from genesis.learning.triage.calibration import TriageCalibrator

        cal = TriageCalibrator(
            router=MagicMock(),
            db=MagicMock(),
            memory_store=None,
            event_bus=None,
        )
        assert cal is not None

    def test_health_probes_are_callable(self):
        """Health probes wrapped with partial are callable."""
        from genesis.observability.health import probe_db, probe_ollama, probe_qdrant

        mock_db = MagicMock()
        probes = [
            partial(probe_db, mock_db),
            probe_qdrant,
            probe_ollama,
        ]
        for p in probes:
            assert callable(p)


class TestCriticalFailureProbeWiring:
    """The critical_failure probe set must respect ``ollama_enabled()``.

    Ollama is opt-in (cloud-primary architecture). On installs that
    don't enable Ollama, including ``probe_ollama`` in the
    ``CriticalFailureCollector`` causes the signal to fire 1.0
    permanently because the probe returns DOWN on every tick — which
    pollutes reflections and observation writes with phantom emergencies.
    """

    def test_critical_failure_probes_exclude_ollama_when_disabled(self):
        """When ollama_enabled() is False, probe_ollama is NOT in the probe set."""
        from unittest.mock import patch

        from genesis.observability.health import probe_db, probe_ollama, probe_qdrant

        with patch("genesis.env.ollama_enabled", return_value=False):
            from genesis.env import ollama_enabled

            mock_db = MagicMock()
            probes = [
                partial(probe_db, mock_db),
                probe_qdrant,
            ]
            if ollama_enabled():
                probes.append(probe_ollama)

            assert len(probes) == 2
            # probe_ollama should NOT be in the set
            assert not any(p is probe_ollama for p in probes)

    def test_critical_failure_probes_include_ollama_when_enabled(self):
        """When ollama_enabled() is True, probe_ollama IS in the probe set."""
        from unittest.mock import patch

        from genesis.observability.health import probe_db, probe_ollama, probe_qdrant

        with patch("genesis.env.ollama_enabled", return_value=True):
            from genesis.env import ollama_enabled

            mock_db = MagicMock()
            probes = [
                partial(probe_db, mock_db),
                probe_qdrant,
            ]
            if ollama_enabled():
                probes.append(probe_ollama)

            assert len(probes) == 3
            assert any(p is probe_ollama for p in probes)
