"""Tests for model intelligence recon job."""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.recon.model_intelligence import ModelIntelligenceJob
from genesis.routing.model_profiles import ModelProfileRegistry


@pytest.fixture()
async def db():
    """In-memory SQLite with observations and follow_ups tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "CREATE TABLE observations ("
            "  id TEXT PRIMARY KEY,"
            "  source TEXT, type TEXT, category TEXT,"
            "  content TEXT, priority TEXT, created_at TEXT,"
            "  resolved_at TEXT, resolution_notes TEXT"
            ")"
        )
        from genesis.db.schema import TABLES
        await conn.execute(TABLES["follow_ups"])
        await conn.commit()
        yield conn


@pytest.fixture()
def registry(tmp_path) -> ModelProfileRegistry:
    """Load a registry with test profiles."""
    p = tmp_path / "profiles.yaml"
    p.write_text(textwrap.dedent("""\
        profiles:
          test-model:
            display_name: "Test Model"
            provider: test
            api_id: "test/test-model"
            intelligence_tier: A
            reasoning: A
            instruction_following: A
            anti_sycophancy: B
            context_window: 200000
            cost_tier: moderate
            cost_per_mtok_in: 2.00
            cost_per_mtok_out: 10.00
            latency: moderate
            best_for: [deep_reflection]
            avoid_for: []
            free_tier:
              available: false
            last_reviewed: "2025-01-01"
            review_source: manual
    """))
    reg = ModelProfileRegistry(p)
    reg.load()
    return reg


def _mock_openrouter_response(models: list[dict]):
    """Create a mock httpx response for OpenRouter API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": models}
    return mock_resp


class TestOpenRouterCheck:
    """OpenRouter model scanning."""

    @pytest.mark.asyncio
    async def test_detects_new_model(self, db, registry) -> None:
        """New model with 100k+ context is flagged."""
        models = [
            {
                "id": "new-provider/new-model",
                "name": "New Model",
                "context_length": 500_000,
                "pricing": {"prompt": "0.000001", "completion": "0.000005"},
            },
        ]

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["openrouter_findings"] >= 1
        new_model_findings = [
            f for f in result["findings"] if f["type"] == "new_model"
        ]
        assert len(new_model_findings) == 1
        assert new_model_findings[0]["api_id"] == "new-provider/new-model"

    @pytest.mark.asyncio
    async def test_detects_pricing_change(self, db, registry) -> None:
        """Pricing change on known model is flagged."""
        models = [
            {
                "id": "test/test-model",
                "name": "Test Model",
                "context_length": 200_000,
                "pricing": {
                    "prompt": "0.000003",  # 3.00/MTok vs 2.00 in profile
                    "completion": "0.000010",  # same
                },
            },
        ]

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        pricing_findings = [
            f for f in result["findings"] if f["type"] == "pricing_change"
        ]
        assert len(pricing_findings) == 1
        assert pricing_findings[0]["model"] == "test-model"

    @pytest.mark.asyncio
    async def test_empty_response_handled(self, db) -> None:
        """Empty OpenRouter response produces no findings."""
        job = ModelIntelligenceJob(db=db)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response([]))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["openrouter_findings"] == 0

    @pytest.mark.asyncio
    async def test_api_failure_handled(self, db) -> None:
        """OpenRouter API failure is handled gracefully."""
        job = ModelIntelligenceJob(db=db)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("down"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["openrouter_findings"] == 0


class TestStalenessCheck:
    """Profile staleness detection."""

    @pytest.mark.asyncio
    async def test_stale_profile_flagged(self, db, registry) -> None:
        """Profile with last_reviewed > 30 days ago is flagged."""
        job = ModelIntelligenceJob(db=db, profile_registry=registry)

        # Mock OpenRouter to return empty
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response([]))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        stale = [f for f in result["findings"] if f["type"] == "stale_profile"]
        assert len(stale) == 1  # test-model was reviewed 2025-01-01
        assert stale[0]["model"] == "test-model"

    @pytest.mark.asyncio
    async def test_no_registry_no_staleness(self, db) -> None:
        """Without registry, no staleness findings."""
        job = ModelIntelligenceJob(db=db)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response([]))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        assert result["stale_findings"] == 0


class TestReconFollowUpPipeline:
    """Recon creates follow-ups for new free models."""

    @pytest.mark.asyncio
    async def test_new_free_model_creates_follow_up(self, db, registry, tmp_path) -> None:
        """New free model creates a follow-up with structured payload."""
        import json

        models = [
            {
                "id": "new-provider/free-model",
                "name": "Free Model",
                "context_length": 100_000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]

        # Use tmp_path for cache to avoid polluting real cache
        cache_path = tmp_path / "free_model_cache.json"

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("genesis.recon.model_intelligence._FREE_MODEL_CACHE_PATH", cache_path),
        ):
            await job.run()

        # Check follow-up was created
        cursor = await db.execute(
            "SELECT * FROM follow_ups WHERE source = 'recon_pipeline'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1

        fu = dict(rows[0])
        assert fu["strategy"] == "surplus_task"
        assert "new-provider/free-model" in fu["content"]
        assert fu["status"] == "pending"

        # Verify structured payload in reason
        payload = json.loads(fu["reason"])
        assert payload["task_type"] == "model_eval"
        assert payload["payload"]["model_id"] == "new-provider/free-model"

    @pytest.mark.asyncio
    async def test_no_follow_up_without_surplus_queue(self, db, registry, tmp_path) -> None:
        """Without surplus queue, follow-up is still created (no cap check)."""
        models = [
            {
                "id": "another/free-model",
                "name": "Another Free",
                "context_length": 50_000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]

        cache_path = tmp_path / "free_model_cache.json"
        job = ModelIntelligenceJob(db=db, profile_registry=registry)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("genesis.recon.model_intelligence._FREE_MODEL_CACHE_PATH", cache_path),
        ):
            await job.run()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM follow_ups WHERE source = 'recon_pipeline'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 1  # follow-up created even without queue


class TestFindingStorage:
    """Finding persistence in observations."""

    @pytest.mark.asyncio
    async def test_findings_stored_in_db(self, db, registry) -> None:
        """Findings are persisted to observations table."""
        models = [
            {
                "id": "new/big-model",
                "name": "Big Model",
                "context_length": 200_000,
                "pricing": {"prompt": "0.000001", "completion": "0.000005"},
            },
        ]

        job = ModelIntelligenceJob(db=db, profile_registry=registry)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_openrouter_response(models))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await job.run()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE category = 'model_intelligence'"
        )
        count = (await cursor.fetchone())[0]
        assert count == result["total_findings"]
        assert count > 0
