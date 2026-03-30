"""Init function: _init_providers."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def init(rt: GenesisRuntime) -> None:
    """Initialize ProviderRegistry and register available providers."""
    try:
        from genesis.env import ollama_enabled
        from genesis.providers.registry import ProviderRegistry
        from genesis.research.orchestrator import ResearchOrchestrator
        from genesis.research.web_adapter import WebSearchAdapter

        rt._provider_registry = ProviderRegistry(
            db=rt._db, event_bus=rt._event_bus
        )

        rt._provider_registry.register(WebSearchAdapter())

        if os.environ.get("API_KEY_GROQ"):
            from genesis.providers.stt import GroqSTTAdapter

            rt._provider_registry.register(GroqSTTAdapter())

        if ollama_enabled():
            from genesis.providers.embedding import OllamaEmbeddingAdapter

            rt._provider_registry.register(OllamaEmbeddingAdapter())

        if os.environ.get("API_KEY_DEEPINFRA"):
            from genesis.providers.embedding import CloudEmbeddingAdapter

            rt._provider_registry.register(CloudEmbeddingAdapter(provider="deepinfra"))

        if os.environ.get("API_KEY_QWEN"):
            from genesis.providers.embedding import CloudEmbeddingAdapter

            rt._provider_registry.register(CloudEmbeddingAdapter(provider="dashscope"))

        from genesis.channels.tts_config import TTSConfigLoader

        tts_config_loader = TTSConfigLoader()

        if os.environ.get("API_KEY_FISH_AUDIO"):
            from genesis.providers.tts import FishAudioTTSAdapter

            rt._provider_registry.register(FishAudioTTSAdapter(config_loader=tts_config_loader))

        if os.environ.get("API_KEY_CARTESIA"):
            from genesis.providers.tts import CartesiaTTSAdapter

            rt._provider_registry.register(CartesiaTTSAdapter(config_loader=tts_config_loader))

        if os.environ.get("API_KEY_ELEVENLABS"):
            from genesis.providers.tts import ElevenLabsTTSAdapter

            rt._provider_registry.register(ElevenLabsTTSAdapter(config_loader=tts_config_loader))

        if os.environ.get("API_KEY_CLOUDFLARE"):
            from genesis.providers.cloudflare_crawl import CloudflareCrawlAdapter

            rt._provider_registry.register(CloudflareCrawlAdapter())

        if os.environ.get("API_KEY_PERPLEXITY"):
            from genesis.research.perplexity import PerplexityAdapter

            rt._provider_registry.register(PerplexityAdapter())

        from genesis.providers.health import QdrantProbeAdapter

        rt._provider_registry.register(QdrantProbeAdapter())
        if ollama_enabled():
            from genesis.providers.health import OllamaProbeAdapter

            rt._provider_registry.register(OllamaProbeAdapter())

        rt._research_orchestrator = ResearchOrchestrator(
            registry=rt._provider_registry,
            event_bus=rt._event_bus,
        )

        count = len(rt._provider_registry.list_all())
        logger.info("Provider registry initialized (%d providers)", count)

        # Warn if no EMBEDDING provider registered — memory subsystem will silently
        # fall back to FTS5-only.
        from genesis.providers.registry import ProviderType  # noqa: PLC0415

        if not rt._provider_registry.list_by_type(ProviderType.EMBEDDING):
            logger.warning(
                "No EMBEDDING provider registered — memory will use FTS5-only fallback. "
                "Set API_KEY_DEEPINFRA, API_KEY_QWEN, or GENESIS_ENABLE_OLLAMA=true "
                "to enable semantic search."
            )

    except ImportError:
        logger.warning("genesis.providers not available")
    except Exception:
        logger.exception("Failed to initialize provider registry")
