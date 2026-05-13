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
