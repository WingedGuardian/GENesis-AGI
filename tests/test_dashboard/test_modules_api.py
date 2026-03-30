"""Tests for modules and providers-detail dashboard API endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


# ── /api/genesis/modules ─────────────────────────────────────────────────


class TestModulesEndpoint:
    def test_empty_when_not_bootstrapped(self, client):
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = False
        mock_rt.module_registry = None

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/modules")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_returns_modules_when_bootstrapped(self, client):
        mock_mod = MagicMock()
        mock_mod.name = "prediction_markets"
        mock_mod.enabled = True
        mock_mod.get_research_profile_name.return_value = "prediction-markets"
        mock_mod._tracker.stats.return_value = {
            "total_records": 5,
            "brier_score": 0.25,
        }

        mock_registry = MagicMock()
        mock_registry.list_modules.return_value = ["prediction_markets"]
        mock_registry.get.return_value = mock_mod

        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt.module_registry = mock_registry

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/modules")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["name"] == "prediction_markets"
            assert data[0]["enabled"] is True
            assert data[0]["research_profile"] == "prediction-markets"
            assert data[0]["stats"]["total_records"] == 5

    def test_module_without_tracker(self, client):
        """Modules that lack a _tracker attribute still return data."""
        mock_mod = MagicMock(spec=["name", "enabled", "get_research_profile_name"])
        mock_mod.name = "test_module"
        mock_mod.enabled = False
        mock_mod.get_research_profile_name.return_value = None

        mock_registry = MagicMock()
        mock_registry.list_modules.return_value = ["test_module"]
        mock_registry.get.return_value = mock_mod

        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt.module_registry = mock_registry

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/modules")
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["name"] == "test_module"
            assert "stats" not in data[0]

    def test_module_registry_none(self, client):
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt.module_registry = None

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/modules")
            assert resp.get_json() == []


# ── /api/genesis/providers-detail ────────────────────────────────────────


class TestProvidersDetailEndpoint:
    def test_empty_when_not_bootstrapped(self, client):
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = False
        mock_rt.provider_registry = None

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/providers-detail")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_returns_providers(self, client):
        from genesis.providers.types import (
            CostTier,
            ProviderCapability,
            ProviderCategory,
            ProviderInfo,
            ProviderStatus,
        )

        mock_provider = MagicMock()
        mock_provider.name = "ollama_embedding"
        mock_provider.capability = ProviderCapability(
            content_types=("text",),
            categories=(ProviderCategory.EMBEDDING,),
            cost_tier=CostTier.FREE,
            description="Ollama local embeddings",
        )

        mock_info = ProviderInfo(
            name="ollama_embedding",
            capability=mock_provider.capability,
            status=ProviderStatus.AVAILABLE,
            invocation_count=42,
            last_used="2026-03-21T12:00:00",
        )

        mock_registry = MagicMock()
        mock_registry.list_all.return_value = [mock_provider]
        mock_registry.info.return_value = mock_info

        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt.provider_registry = mock_registry

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/providers-detail")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["name"] == "ollama_embedding"
            assert data[0]["cost_tier"] == "free"
            assert data[0]["status"] == "available"
            assert data[0]["invocation_count"] == 42

    def test_provider_registry_none(self, client):
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt.provider_registry = None

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/providers-detail")
            assert resp.get_json() == []
