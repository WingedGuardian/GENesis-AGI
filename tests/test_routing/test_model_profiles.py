"""Tests for model profile registry — loader, matcher, staleness detection."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from genesis.routing.model_profiles import (
    COST_TIERS,
    TIER_SCORES,
    ModelProfileRegistry,
)


@pytest.fixture()
def profiles_yaml(tmp_path: Path) -> Path:
    """Write a minimal model profiles YAML and return its path."""
    p = tmp_path / "model_profiles.yaml"
    p.write_text(textwrap.dedent("""\
        profiles:
          model-a:
            display_name: "Model A"
            provider: alpha
            api_id: "alpha/model-a"
            intelligence_tier: S
            reasoning: S
            instruction_following: A
            anti_sycophancy: A
            context_window: 1000000
            cost_tier: moderate
            cost_per_mtok_in: 2.50
            cost_per_mtok_out: 12.00
            latency: moderate
            strengths:
              - reasoning
              - planning
            weaknesses:
              - creative writing
            best_for: [deep_reflection, code_work]
            avoid_for: [surplus_brainstorm]
            free_tier:
              available: false
            last_reviewed: "2026-03-14"
            review_source: manual

          model-b:
            display_name: "Model B"
            provider: beta
            api_id: "beta/model-b"
            intelligence_tier: B
            reasoning: B
            instruction_following: A
            anti_sycophancy: C
            context_window: 131072
            cost_tier: cheap
            cost_per_mtok_in: 0.50
            cost_per_mtok_out: 3.00
            latency: low
            strengths:
              - speed
            best_for: [micro_reflection, surplus_brainstorm]
            avoid_for: [deep_reflection]
            free_tier:
              available: true
              provider: some_free
              limits: "100 RPM"
            last_reviewed: "2026-01-01"
            review_source: manual

          model-c:
            display_name: "Model C"
            provider: gamma
            api_id: "gamma/model-c"
            intelligence_tier: A
            reasoning: A
            instruction_following: A
            anti_sycophancy: B
            context_window: 200000
            cost_tier: expensive
            cost_per_mtok_in: 5.00
            cost_per_mtok_out: 25.00
            latency: high
            best_for: [strategic_reflection, quality_calibration]
            avoid_for: [micro_reflection]
            free_tier:
              available: false
            last_reviewed: "2026-03-14"
            review_source: manual
    """))
    return p


@pytest.fixture()
def registry(profiles_yaml: Path) -> ModelProfileRegistry:
    """Load a registry from the test YAML."""
    reg = ModelProfileRegistry(profiles_yaml)
    reg.load()
    return reg


class TestLoading:
    """Profile loading from YAML."""

    def test_loads_all_profiles(self, registry: ModelProfileRegistry) -> None:
        assert len(registry.all_profiles()) == 3

    def test_profile_fields_parsed(self, registry: ModelProfileRegistry) -> None:
        a = registry.get("model-a")
        assert a is not None
        assert a.display_name == "Model A"
        assert a.provider == "alpha"
        assert a.api_id == "alpha/model-a"
        assert a.intelligence_tier == "S"
        assert a.reasoning == "S"
        assert a.instruction_following == "A"
        assert a.anti_sycophancy == "A"
        assert a.context_window == 1_000_000
        assert a.cost_tier == "moderate"
        assert a.cost_per_mtok_in == 2.50
        assert a.cost_per_mtok_out == 12.00
        assert a.latency == "moderate"
        assert "reasoning" in a.strengths
        assert "creative writing" in a.weaknesses
        assert "deep_reflection" in a.best_for
        assert "surplus_brainstorm" in a.avoid_for
        assert a.free_tier == {"available": False}
        assert a.last_reviewed == "2026-03-14"

    def test_free_tier_parsed(self, registry: ModelProfileRegistry) -> None:
        b = registry.get("model-b")
        assert b is not None
        assert b.free_tier["available"] is True
        assert b.free_tier["provider"] == "some_free"

    def test_get_nonexistent_returns_none(self, registry: ModelProfileRegistry) -> None:
        assert registry.get("nonexistent") is None

    def test_empty_file_handles_gracefully(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("profiles: {}")
        reg = ModelProfileRegistry(p)
        reg.load()
        assert len(reg.all_profiles()) == 0

    def test_missing_file_handles_gracefully(self, tmp_path: Path) -> None:
        p = tmp_path / "missing.yaml"
        reg = ModelProfileRegistry(p)
        reg.load()
        assert len(reg.all_profiles()) == 0

    def test_missing_optional_fields_have_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "minimal.yaml"
        p.write_text(textwrap.dedent("""\
            profiles:
              bare:
                provider: test
                api_id: "test/bare"
        """))
        reg = ModelProfileRegistry(p)
        reg.load()
        bare = reg.get("bare")
        assert bare is not None
        assert bare.intelligence_tier == "C"
        assert bare.reasoning == "C"
        assert bare.strengths == ()
        assert bare.best_for == ()
        assert bare.free_tier == {"available": False}

    def test_real_profiles_load(self) -> None:
        """Load the real config/model_profiles.yaml if it exists."""
        real = Path(__file__).resolve().parents[2] / "config" / "model_profiles.yaml"
        if not real.exists():
            pytest.skip("Real model_profiles.yaml not found")
        reg = ModelProfileRegistry(real)
        reg.load()
        assert len(reg.all_profiles()) >= 15


class TestMatcher:
    """Model matching for task types."""

    def test_deep_reflection_prefers_s_tier(self, registry: ModelProfileRegistry) -> None:
        results = registry.match("deep_reflection")
        assert len(results) >= 1
        # model-a (S-tier reasoning, best_for deep_reflection) should be first
        assert results[0].name == "model-a"

    def test_avoid_for_excludes(self, registry: ModelProfileRegistry) -> None:
        results = registry.match("surplus_brainstorm")
        names = [r.name for r in results]
        assert "model-a" not in names  # model-a avoid_for surplus_brainstorm

    def test_require_free_filters(self, registry: ModelProfileRegistry) -> None:
        results = registry.match("micro_reflection", require_free=True)
        for r in results:
            assert r.free_tier.get("available") is True
        names = [r.name for r in results]
        assert "model-b" in names
        assert "model-a" not in names

    def test_max_cost_tier_filters(self, registry: ModelProfileRegistry) -> None:
        results = registry.match("deep_reflection", max_cost_tier="moderate")
        for r in results:
            assert COST_TIERS[r.cost_tier] <= COST_TIERS["moderate"]
        names = [r.name for r in results]
        assert "model-c" not in names  # expensive

    def test_min_reasoning_filters(self, registry: ModelProfileRegistry) -> None:
        results = registry.match("deep_reflection", min_reasoning="A")
        for r in results:
            assert TIER_SCORES[r.reasoning] >= TIER_SCORES["A"]
        names = [r.name for r in results]
        assert "model-b" not in names  # B-tier reasoning

    def test_min_anti_sycophancy_filters(self, registry: ModelProfileRegistry) -> None:
        results = registry.match("deep_reflection", min_anti_sycophancy="A")
        for r in results:
            assert TIER_SCORES[r.anti_sycophancy] >= TIER_SCORES["A"]

    def test_exclude_providers(self, registry: ModelProfileRegistry) -> None:
        results = registry.match("deep_reflection", exclude_providers=["alpha"])
        names = [r.name for r in results]
        assert "model-a" not in names

    def test_circuit_breaker_excludes_unhealthy(
        self, registry: ModelProfileRegistry
    ) -> None:
        mock_cb_registry = MagicMock()
        unhealthy_cb = MagicMock()
        unhealthy_cb.is_available.return_value = False
        healthy_cb = MagicMock()
        healthy_cb.is_available.return_value = True

        def get_cb(provider: str):
            if provider == "alpha":
                return unhealthy_cb
            return healthy_cb

        mock_cb_registry.get = get_cb

        results = registry.match("deep_reflection", circuit_breakers=mock_cb_registry)
        names = [r.name for r in results]
        assert "model-a" not in names

    def test_best_for_bonus(self, registry: ModelProfileRegistry) -> None:
        # model-b has best_for micro_reflection; model-c avoids it
        results = registry.match("micro_reflection")
        if len(results) >= 1:
            # model-b should appear despite lower tier due to best_for bonus
            names = [r.name for r in results]
            assert "model-b" in names

    def test_unknown_task_type_uses_defaults(
        self, registry: ModelProfileRegistry
    ) -> None:
        results = registry.match("unknown_task_type")
        # Should still return results using default weights
        assert len(results) >= 1


class TestStaleness:
    """Stale profile detection."""

    def test_stale_detected(self, registry: ModelProfileRegistry) -> None:
        # model-b last reviewed 2026-01-01 — over 30 days ago from 2026-03-14
        stale = registry.stale_profiles(days=30)
        names = [p.name for p in stale]
        assert "model-b" in names

    def test_fresh_not_stale(self, registry: ModelProfileRegistry) -> None:
        # model-a and model-c reviewed 2026-03-14 — fresh
        stale = registry.stale_profiles(days=30)
        names = [p.name for p in stale]
        assert "model-a" not in names
        assert "model-c" not in names

    def test_missing_date_is_stale(self, tmp_path: Path) -> None:
        p = tmp_path / "no_date.yaml"
        p.write_text(textwrap.dedent("""\
            profiles:
              no-date:
                provider: test
                api_id: "test/nd"
        """))
        reg = ModelProfileRegistry(p)
        reg.load()
        stale = reg.stale_profiles(days=30)
        assert len(stale) == 1
        assert stale[0].name == "no-date"
