"""STT provider protocol and Groq adapter.

Wraps genesis.channels.stt as a ToolProvider.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Protocol, runtime_checkable

from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class STTProvider(ToolProvider, Protocol):
    """Specialized ToolProvider for speech-to-text."""

    async def transcribe(self, audio_bytes: bytes, *, model: str | None = None) -> str: ...


class GroqSTTAdapter:
    """Wraps genesis.channels.stt as a ToolProvider."""

    name = "groq_stt"
    capability = ProviderCapability(
        content_types=("audio",),
        categories=(ProviderCategory.STT,),
        cost_tier=CostTier.CHEAP,
        description="Speech-to-text via Groq Whisper API",
    )

    async def check_health(self) -> ProviderStatus:
        if os.environ.get("API_KEY_GROQ"):
            return ProviderStatus.AVAILABLE
        return ProviderStatus.UNAVAILABLE

    async def transcribe(self, audio_bytes: bytes, *, model: str | None = None) -> str:
        from genesis.channels.stt import transcribe

        return await transcribe(audio_bytes, model_name=model or "whisper-large-v3")

    async def invoke(self, request: dict) -> ProviderResult:
        start = time.monotonic()
        try:
            audio = request.get("audio_bytes", b"")
            model = request.get("model")
            text = await self.transcribe(audio, model=model)
            latency = (time.monotonic() - start) * 1000
            return ProviderResult(
                success=bool(text),
                data=text,
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
