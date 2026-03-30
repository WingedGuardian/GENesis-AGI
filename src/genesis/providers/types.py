"""Provider type definitions — enums and frozen dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProviderCategory(StrEnum):
    SEARCH = "search"
    STT = "stt"
    TTS = "tts"
    EMBEDDING = "embedding"
    WEB = "web"
    ANALYSIS = "analysis"
    EXTRACTION = "extraction"
    HEALTH = "health"
    DATA_ACCESS = "data_access"


class CostTier(StrEnum):
    FREE = "free"
    CHEAP = "cheap"
    MODERATE = "moderate"
    EXPENSIVE = "expensive"


class ProviderStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProviderCapability:
    """Declares what a provider can do."""

    content_types: tuple[str, ...] = ()
    categories: tuple[ProviderCategory, ...] = ()
    cost_tier: CostTier = CostTier.FREE
    description: str = ""


@dataclass(frozen=True)
class ProviderResult:
    """Result from a single provider invocation."""

    success: bool
    data: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    provider_name: str = ""


@dataclass(frozen=True)
class ProviderInfo:
    """Read-only snapshot of a registered provider's state."""

    name: str
    capability: ProviderCapability
    status: ProviderStatus = ProviderStatus.AVAILABLE
    invocation_count: int = 0
    last_used: str = ""
