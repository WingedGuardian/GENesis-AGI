"""Tests for genesis.pipeline.orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock

from genesis.pipeline.collectors import CollectorRegistry
from genesis.pipeline.orchestrator import PipelineOrchestrator
from genesis.pipeline.profiles import ProfileLoader, ResearchProfile, SourceConfig
from genesis.pipeline.triage import TriageFilter
from genesis.pipeline.types import (
    CollectorResult,
    PipelineRunResult,
    ResearchSignal,
    SignalStatus,
    Tier,
)


def _make_signal(content: str, **kwargs) -> ResearchSignal:
    defaults = {
        "id": f"sig-{id(content)}",
        "source": "test",
        "profile_name": "test_profile",
        "content": content,
        "tier": Tier.COLLECTION,
        "status": SignalStatus.RAW,
    }
    defaults.update(kwargs)
    return ResearchSignal(**defaults)


def _make_orchestrator(
    profiles: dict[str, ResearchProfile] | None = None,
    collector_registry: CollectorRegistry | None = None,
    triage_filter: TriageFilter | None = None,
    memory_store=None,
) -> PipelineOrchestrator:
    loader = ProfileLoader.__new__(ProfileLoader)
    loader._config_dir = None
    loader._profiles = profiles or {}
    return PipelineOrchestrator(
        profile_loader=loader,
        collector_registry=collector_registry or CollectorRegistry(),
        triage_filter=triage_filter or TriageFilter(),
        memory_store=memory_store,
    )


class TestPipelineOrchestratorCollection:
    async def test_run_collection_with_mock_collector(self):
        profile = ResearchProfile(
            name="test_profile",
            sources=[SourceConfig(name="mock_src", type="mock", queries=["q1"])],
        )

        mock_collector = AsyncMock()
        mock_collector.collect.return_value = CollectorResult(
            collector_name="mock",
            signals=[_make_signal("Result 1"), _make_signal("Result 2")],
        )

        registry = CollectorRegistry()

        class MockCollectorCls:
            name = "mock"

            def __init__(self, profile_name, **kw):
                pass

            async def collect(self, queries, *, max_results=20):
                return await mock_collector.collect(queries, max_results=max_results)

        registry.register("mock", MockCollectorCls)

        orch = _make_orchestrator(
            profiles={"test_profile": profile},
            collector_registry=registry,
        )
        result = await orch.run_collection("test_profile")
        assert isinstance(result, PipelineRunResult)
        assert result.tier0_collected == 2

    async def test_collector_errors_captured_in_result(self):
        profile = ResearchProfile(
            name="test_profile",
            sources=[SourceConfig(name="bad", type="bad_type", queries=["q"])],
        )
        orch = _make_orchestrator(profiles={"test_profile": profile})
        result = await orch.run_collection("test_profile")
        assert len(result.errors) > 0

    async def test_unknown_profile_returns_error(self):
        orch = _make_orchestrator()
        result = await orch.run_collection("nonexistent")
        assert len(result.errors) > 0
        assert "not found" in result.errors[0].lower()


class TestPipelineOrchestratorCycle:
    async def test_run_cycle_collects_and_triages(self):
        profile = ResearchProfile(
            name="test_profile",
            sources=[SourceConfig(name="mock_src", type="mock", queries=["q"])],
            relevance_keywords=["bitcoin"],
        )

        mock_collector = AsyncMock()
        mock_collector.collect.return_value = CollectorResult(
            collector_name="mock",
            signals=[
                _make_signal("Bitcoin price surges"),
                _make_signal("Cat videos trending"),
            ],
        )

        registry = CollectorRegistry()

        class MockCollectorCls:
            name = "mock"

            def __init__(self, profile_name, **kw):
                pass

            async def collect(self, queries, *, max_results=20):
                return await mock_collector.collect(queries, max_results=max_results)

        registry.register("mock", MockCollectorCls)

        orch = _make_orchestrator(
            profiles={"test_profile": profile},
            collector_registry=registry,
        )
        result = await orch.run_cycle("test_profile")
        assert result.tier0_collected == 2
        # Keyword triage: only "bitcoin" should survive
        assert result.tier1_survived == 1
        assert result.discarded == 1

    async def test_run_cycle_stores_surviving_signals(self):
        profile = ResearchProfile(
            name="test_profile",
            sources=[SourceConfig(name="mock_src", type="mock", queries=["q"])],
            relevance_keywords=["important"],
        )

        mock_collector = AsyncMock()
        mock_collector.collect.return_value = CollectorResult(
            collector_name="mock",
            signals=[_make_signal("Important finding here")],
        )

        registry = CollectorRegistry()

        class MockCollectorCls:
            name = "mock"

            def __init__(self, profile_name, **kw):
                pass

            async def collect(self, queries, *, max_results=20):
                return await mock_collector.collect(queries, max_results=max_results)

        registry.register("mock", MockCollectorCls)

        mock_memory = AsyncMock()
        orch = _make_orchestrator(
            profiles={"test_profile": profile},
            collector_registry=registry,
            memory_store=mock_memory,
        )
        await orch.run_cycle("test_profile")
        mock_memory.store.assert_called_once()

    async def test_unknown_profile_cycle_returns_error(self):
        orch = _make_orchestrator()
        result = await orch.run_cycle("nonexistent")
        assert len(result.errors) > 0
