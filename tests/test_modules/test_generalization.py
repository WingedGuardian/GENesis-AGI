"""Tests for genesis.modules.generalization — GeneralizationFilter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from genesis.modules.generalization import GeneralizationFilter


class TestGeneralizationFilterEvaluate:
    async def test_evaluate_returns_none_when_no_router(self):
        gf = GeneralizationFilter()
        result = await gf.evaluate("mod", {"action": "buy"})
        assert result is None

    async def test_evaluate_with_generalizable_result(self):
        gf = GeneralizationFilter()
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "generalizable": True,
            "lesson": "Breaking research into sub-questions improved quality",
            "category": "process",
        })
        result = await gf.evaluate("crypto_ops", {"action": "buy"}, router=router)
        assert result is not None
        assert result["source"] == "module:crypto_ops"
        assert "sub-questions" in result["lesson"]
        assert result["category"] == "process"

    async def test_evaluate_with_non_generalizable_result(self):
        gf = GeneralizationFilter()
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "generalizable": False,
            "reason": "Domain-specific pattern",
        })
        result = await gf.evaluate("mod", {"action": "buy"}, router=router)
        assert result is None

    async def test_evaluate_handles_llm_errors_gracefully(self):
        gf = GeneralizationFilter()
        router = AsyncMock()
        router.route.side_effect = RuntimeError("LLM unavailable")
        result = await gf.evaluate("mod", {"action": "buy"}, router=router)
        assert result is None


class TestGeneralizationFilterPromote:
    async def test_promote_to_core_writes_observation(self):
        writer = AsyncMock()
        writer.write.return_value = "obs-123"
        gf = GeneralizationFilter(observation_writer=writer)
        db = AsyncMock()
        lesson = {
            "source": "module:crypto_ops",
            "lesson": "Sub-questions improve research quality",
            "category": "process",
        }
        obs_id = await gf.promote_to_core(lesson, db=db)
        assert obs_id == "obs-123"
        writer.write.assert_called_once()

    async def test_promote_to_core_returns_none_when_no_writer(self):
        gf = GeneralizationFilter()
        result = await gf.promote_to_core(
            {"source": "mod", "lesson": "test"}, db=AsyncMock(),
        )
        assert result is None

    async def test_promote_to_core_returns_none_when_no_db(self):
        writer = AsyncMock()
        gf = GeneralizationFilter(observation_writer=writer)
        result = await gf.promote_to_core({"source": "mod", "lesson": "test"})
        assert result is None
