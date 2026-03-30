"""Tests for NarrativeDetector."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from genesis.modules.crypto_ops.narrative import NarrativeDetector
from genesis.modules.crypto_ops.types import Narrative, NarrativeStatus


class TestNarrativeDetector:
    def test_empty_initially(self):
        detector = NarrativeDetector()
        assert detector.narrative_count == 0
        assert detector.active_narratives == []

    def test_add_and_retrieve(self):
        detector = NarrativeDetector()
        n = Narrative(name="AI agents", momentum_score=0.8)
        detector.add_narrative(n)
        assert detector.narrative_count == 1
        assert detector.get_narrative(n.id) is n

    def test_active_excludes_fading(self):
        detector = NarrativeDetector()
        n1 = Narrative(name="Active", momentum_score=0.6, status=NarrativeStatus.BUILDING)
        n2 = Narrative(name="Fading", momentum_score=0.1, status=NarrativeStatus.FADING)
        detector.add_narrative(n1)
        detector.add_narrative(n2)
        active = detector.active_narratives
        assert len(active) == 1
        assert active[0].name == "Active"

    def test_update_momentum_transitions_status(self):
        detector = NarrativeDetector()
        n = Narrative(name="Test", momentum_score=0.3)
        detector.add_narrative(n)

        detector.update_momentum(n.id, 0.8)
        assert n.status == NarrativeStatus.PEAKING

        detector.update_momentum(n.id, 0.5)
        assert n.status == NarrativeStatus.BUILDING

        detector.update_momentum(n.id, 0.05)
        assert n.status == NarrativeStatus.FADING

    async def test_detect_with_router(self):
        router = AsyncMock()
        router.route.return_value = json.dumps([{
            "name": "AI agents on Solana",
            "description": "Growing trend of AI agent tokens",
            "momentum": 0.75,
            "signals": ["Multiple AI token launches", "Twitter buzz"],
            "categories": ["AI", "Solana"],
        }])
        detector = NarrativeDetector()
        result = await detector.detect(["signal1", "signal2"], router=router)
        assert len(result) == 1
        assert result[0].name == "AI agents on Solana"
        assert result[0].momentum_score == 0.75
        assert result[0].status == NarrativeStatus.PEAKING

    async def test_detect_no_router_returns_empty(self):
        detector = NarrativeDetector()
        result = await detector.detect(["signal"])
        assert result == []

    async def test_detect_no_signals_returns_empty(self):
        detector = NarrativeDetector()
        result = await detector.detect([], router=AsyncMock())
        assert result == []

    async def test_detect_handles_error(self):
        router = AsyncMock()
        router.route.side_effect = RuntimeError("fail")
        detector = NarrativeDetector()
        result = await detector.detect(["sig"], router=router)
        assert result == []
