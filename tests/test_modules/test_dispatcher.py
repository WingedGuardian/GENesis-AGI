"""Tests for ModuleDispatcher — routes pipeline signals to modules."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.modules.dispatcher import ModuleDispatcher
from genesis.modules.registry import ModuleRegistry
from genesis.pipeline.types import ResearchSignal


def _make_signal(content: str = "test signal") -> ResearchSignal:
    return ResearchSignal(
        id="sig-1",
        source="test",
        profile_name="test-profile",
        content=content,
        collected_at="2026-01-01T00:00:00Z",
    )


class TestModuleDispatcher:
    @pytest.mark.asyncio()
    async def test_dispatch_to_signal_based_module(self):
        """Signal-based module (crypto_ops) receives signals."""
        mod = MagicMock()
        mod.name = "crypto_ops"
        mod.enabled = True
        mod.get_research_profile_name.return_value = "crypto-ops"
        mod.handle_opportunity = AsyncMock(return_value={"type": "crypto_narrative"})
        mod.register = AsyncMock()
        # No _scanner attribute
        del mod._scanner

        registry = ModuleRegistry()
        await registry.load_module(mod)

        dispatcher = ModuleDispatcher(registry)
        signals = [_make_signal("trending meme coin")]
        result = await dispatcher.dispatch("crypto-ops", signals, router=MagicMock())

        assert result == {"type": "crypto_narrative"}
        mod.handle_opportunity.assert_awaited_once()
        call_args = mod.handle_opportunity.call_args[0][0]
        assert call_args["signals"] == ["trending meme coin"]

    @pytest.mark.asyncio()
    async def test_dispatch_to_scanner_based_module(self):
        """Scanner-based module (prediction_markets) triggers scan."""
        market = MagicMock()
        scanner = MagicMock()
        scanner.scan = AsyncMock(return_value=[market])

        mod = MagicMock()
        mod.name = "prediction_markets"
        mod.enabled = True
        mod.get_research_profile_name.return_value = "prediction-markets"
        mod._scanner = scanner
        mod.handle_opportunity = AsyncMock(return_value={"type": "prediction_market_bet"})
        mod.register = AsyncMock()

        registry = ModuleRegistry()
        await registry.load_module(mod)

        dispatcher = ModuleDispatcher(registry)
        result = await dispatcher.dispatch("prediction-markets", [_make_signal()])

        assert result == {"type": "prediction_market_bet"}
        scanner.scan.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_no_module_for_profile(self):
        """Returns None when no module matches the profile."""
        registry = ModuleRegistry()
        dispatcher = ModuleDispatcher(registry)

        result = await dispatcher.dispatch("unknown-profile", [_make_signal()])
        assert result is None

    @pytest.mark.asyncio()
    async def test_disabled_module_skipped(self):
        """Disabled modules are not dispatched to."""
        mod = MagicMock()
        mod.name = "test"
        mod.enabled = False
        mod.get_research_profile_name.return_value = "test-profile"
        mod.register = AsyncMock()

        registry = ModuleRegistry()
        await registry.load_module(mod)

        dispatcher = ModuleDispatcher(registry)
        result = await dispatcher.dispatch("test-profile", [_make_signal()])
        assert result is None

    @pytest.mark.asyncio()
    async def test_module_returns_none(self):
        """Module that finds no opportunity returns None."""
        mod = MagicMock()
        mod.name = "test"
        mod.enabled = True
        mod.get_research_profile_name.return_value = "test-profile"
        mod.handle_opportunity = AsyncMock(return_value=None)
        mod.register = AsyncMock()
        del mod._scanner

        registry = ModuleRegistry()
        await registry.load_module(mod)

        dispatcher = ModuleDispatcher(registry)
        result = await dispatcher.dispatch("test-profile", [_make_signal()])
        assert result is None

    @pytest.mark.asyncio()
    async def test_scanner_no_markets(self):
        """Scanner returning empty list means no dispatch."""
        scanner = MagicMock()
        scanner.scan = AsyncMock(return_value=[])

        mod = MagicMock()
        mod.name = "pm"
        mod.enabled = True
        mod.get_research_profile_name.return_value = "pm"
        mod._scanner = scanner
        mod.register = AsyncMock()

        registry = ModuleRegistry()
        await registry.load_module(mod)

        dispatcher = ModuleDispatcher(registry)
        result = await dispatcher.dispatch("pm", [_make_signal()])
        assert result is None

    @pytest.mark.asyncio()
    async def test_exception_in_module_handled(self):
        """Exceptions in handle_opportunity don't crash the dispatcher."""
        mod = MagicMock()
        mod.name = "broken"
        mod.enabled = True
        mod.get_research_profile_name.return_value = "broken"
        mod.handle_opportunity = AsyncMock(side_effect=RuntimeError("boom"))
        mod.register = AsyncMock()
        del mod._scanner

        registry = ModuleRegistry()
        await registry.load_module(mod)

        dispatcher = ModuleDispatcher(registry)
        result = await dispatcher.dispatch("broken", [_make_signal()])
        assert result is None  # Graceful failure
