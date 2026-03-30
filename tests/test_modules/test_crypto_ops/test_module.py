"""Tests for CryptoOpsModule."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from genesis.modules.crypto_ops.module import CryptoOpsModule


class TestCryptoOpsModuleProperties:
    def test_name(self):
        mod = CryptoOpsModule()
        assert mod.name == "crypto_ops"

    def test_enabled(self):
        mod = CryptoOpsModule()
        assert mod.enabled is False

    def test_research_profile(self):
        mod = CryptoOpsModule()
        assert mod.get_research_profile_name() == "crypto-ops"


class TestCryptoOpsLifecycle:
    async def test_register_and_deregister(self):
        mod = CryptoOpsModule()
        await mod.register(AsyncMock())
        await mod.deregister()


class TestCryptoOpsHandleOpportunity:
    async def test_returns_none_without_signals(self):
        mod = CryptoOpsModule()
        result = await mod.handle_opportunity({})
        assert result is None

    async def test_returns_proposal_with_narratives(self):
        router = AsyncMock()
        router.route.return_value = json.dumps([{
            "name": "AI meme tokens",
            "description": "Growing trend",
            "momentum": 0.8,
            "signals": ["buzz"],
            "categories": ["AI"],
        }])
        mod = CryptoOpsModule()
        result = await mod.handle_opportunity({
            "signals": ["AI token launches trending"],
            "router": router,
        })
        assert result is not None
        assert result["type"] == "crypto_narrative"
        assert result["requires_approval"] is True


class TestCryptoOpsExtractGeneralizable:
    async def test_extracts_lesson(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "generalizable": True,
            "lesson": "Social signals lag actual market moves by 2-4 hours",
            "category": "source_reliability",
        })
        mod = CryptoOpsModule()
        result = await mod.extract_generalizable({
            "narrative_name": "AI",
            "chain": "solana",
            "token_name": "TEST",
            "pnl_pct": 0.5,
            "narrative_accurate": True,
            "timing": "on-time",
        }, router=router)
        assert result is not None
        assert result[0]["source"] == "module:crypto_ops"

    async def test_returns_none_without_router(self):
        mod = CryptoOpsModule()
        result = await mod.extract_generalizable({})
        assert result is None

    async def test_returns_none_for_non_generalizable(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "generalizable": False,
            "reason": "Market-specific",
        })
        mod = CryptoOpsModule()
        result = await mod.extract_generalizable({
            "narrative_name": "X", "chain": "y", "token_name": "z",
            "pnl_pct": 0, "narrative_accurate": False, "timing": "late",
        }, router=router)
        assert result is None
