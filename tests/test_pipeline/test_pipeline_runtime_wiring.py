"""Tests for PipelineOrchestrator wiring in GenesisRuntime."""

from __future__ import annotations

from genesis.runtime import GenesisRuntime


class TestPipelineRuntimeProperty:
    def test_pipeline_orchestrator_property_exists(self):
        rt = GenesisRuntime()
        assert hasattr(rt, "pipeline_orchestrator")
        assert rt.pipeline_orchestrator is None

    def test_init_checks_includes_pipeline(self):
        assert "pipeline" in GenesisRuntime._INIT_CHECKS
        assert GenesisRuntime._INIT_CHECKS["pipeline"] == "_pipeline_orchestrator"

    def test_run_pipeline_cycle_method_exists(self):
        rt = GenesisRuntime()
        assert hasattr(rt, "_run_pipeline_cycle")
        assert callable(rt._run_pipeline_cycle)
