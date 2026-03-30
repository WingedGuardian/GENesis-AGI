"""TTS provider protocol and adapters (Fish Audio, Cartesia, ElevenLabs).

Wraps genesis.channels.tts as ToolProviders — mirrors genesis.providers.stt.
Adapters accept an optional TTSConfigLoader for hot-reloadable settings.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

if TYPE_CHECKING:
    from genesis.channels.tts_config import TTSConfigLoader

logger = logging.getLogger(__name__)


@runtime_checkable
class TTSProvider(ToolProvider, Protocol):
    """Specialized ToolProvider for text-to-speech."""

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> bytes: ...


# ── Fish Audio ───────────────────────────────────────────────────────────


class FishAudioTTSAdapter:
    """Fish Audio TTS — pay-as-you-go, voice cloning, OGG/Opus output."""

    name = "fish_audio_tts"
    capability = ProviderCapability(
        content_types=("text",),
        categories=(ProviderCategory.TTS,),
        cost_tier=CostTier.CHEAP,
        description="Text-to-speech via Fish Audio API",
    )

    def __init__(self, config_loader: TTSConfigLoader | None = None) -> None:
        self._config_loader = config_loader

    async def check_health(self) -> ProviderStatus:
        if os.environ.get("API_KEY_FISH_AUDIO"):
            return ProviderStatus.AVAILABLE
        return ProviderStatus.UNAVAILABLE

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> bytes:
        from genesis.channels.tts import synthesize_fish

        config = self._config_loader.load() if self._config_loader else None
        return await synthesize_fish(text, voice_id=voice_id, config=config)

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        try:
            text = request.get("text", "")
            voice_id = request.get("voice_id")
            audio = await self.synthesize(text, voice_id=voice_id)
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=bool(audio),
                data=audio,
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )


# ── Cartesia Sonic ───────────────────────────────────────────────────────


class CartesiaTTSAdapter:
    """Cartesia Sonic TTS — fastest latency, credit-based, WAV output."""

    name = "cartesia_tts"
    capability = ProviderCapability(
        content_types=("text",),
        categories=(ProviderCategory.TTS,),
        cost_tier=CostTier.CHEAP,
        description="Text-to-speech via Cartesia Sonic API",
    )

    def __init__(self, config_loader: TTSConfigLoader | None = None) -> None:
        self._config_loader = config_loader

    async def check_health(self) -> ProviderStatus:
        if os.environ.get("API_KEY_CARTESIA"):
            return ProviderStatus.AVAILABLE
        return ProviderStatus.UNAVAILABLE

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> bytes:
        from genesis.channels.tts import synthesize_cartesia

        config = self._config_loader.load() if self._config_loader else None
        return await synthesize_cartesia(text, voice_id=voice_id, config=config)

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        try:
            text = request.get("text", "")
            voice_id = request.get("voice_id")
            audio = await self.synthesize(text, voice_id=voice_id)
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=bool(audio),
                data=audio,
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )


# ── ElevenLabs ───────────────────────────────────────────────────────────


class ElevenLabsTTSAdapter:
    """ElevenLabs TTS — best quality, subscription-based, Opus output."""

    name = "elevenlabs_tts"
    capability = ProviderCapability(
        content_types=("text",),
        categories=(ProviderCategory.TTS,),
        cost_tier=CostTier.MODERATE,
        description="Text-to-speech via ElevenLabs API",
    )

    def __init__(self, config_loader: TTSConfigLoader | None = None) -> None:
        self._config_loader = config_loader

    async def check_health(self) -> ProviderStatus:
        if os.environ.get("API_KEY_ELEVENLABS"):
            return ProviderStatus.AVAILABLE
        return ProviderStatus.UNAVAILABLE

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> bytes:
        from genesis.channels.tts import synthesize_elevenlabs

        config = self._config_loader.load() if self._config_loader else None
        return await synthesize_elevenlabs(text, voice_id=voice_id, config=config)

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        try:
            text = request.get("text", "")
            voice_id = request.get("voice_id")
            audio = await self.synthesize(text, voice_id=voice_id)
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=bool(audio),
                data=audio,
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round(latency, 2),
                provider_name=self.name,
            )
