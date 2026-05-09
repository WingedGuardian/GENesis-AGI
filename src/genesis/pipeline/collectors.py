"""Collector registry — pluggable data source system for the pipeline."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from genesis.pipeline.types import CollectorResult, ResearchSignal

logger = logging.getLogger(__name__)


@runtime_checkable
class Collector(Protocol):
    """Protocol for pipeline data collectors."""

    name: str

    async def collect(self, queries: list[str], *, max_results: int = 20) -> CollectorResult: ...


class WebSearchCollector:
    """Collects from Tinyfish/Brave via genesis.web."""

    name = "web_search"

    def __init__(self, profile_name: str):
        self._profile_name = profile_name

    async def collect(self, queries: list[str], *, max_results: int = 20) -> CollectorResult:
        from genesis.web import search

        signals: list[ResearchSignal] = []
        errors: list[str] = []
        for query in queries:
            try:
                response = await search(query, max_results=max_results)
                for result in response.results:
                    signal = ResearchSignal(
                        id=str(uuid.uuid4()),
                        source="web_search",
                        profile_name=self._profile_name,
                        content=f"{result.title}\n{result.snippet}" if result.snippet else result.title,
                        url=result.url,
                        collected_at=datetime.now(UTC).isoformat(),
                        tags=[query],
                    )
                    signals.append(signal)
            except Exception as e:
                errors.append(f"web_search query '{query}' failed: {e}")
        return CollectorResult(collector_name="web_search", signals=signals, errors=errors)


class CollectorRegistry:
    """Manages available collectors."""

    def __init__(self) -> None:
        self._collectors: dict[str, type] = {}
        # Register built-in collectors
        self._collectors["web_search"] = WebSearchCollector

        # Optional collectors — import lazily to avoid hard dependency
        try:
            from genesis.pipeline.coingecko_collector import CoinGeckoCollector

            self._collectors["coingecko"] = CoinGeckoCollector
        except ImportError:
            pass
        try:
            from genesis.pipeline.dexscreener_collector import DexScreenerCollector

            self._collectors["dexscreener"] = DexScreenerCollector
        except ImportError:
            pass

    def register(self, name: str, collector_cls: type) -> None:
        self._collectors[name] = collector_cls

    def create(self, name: str, profile_name: str, **kwargs: object) -> Collector:
        cls = self._collectors.get(name)
        if cls is None:
            raise KeyError(f"Unknown collector: {name}")
        return cls(profile_name=profile_name, **kwargs)

    def available(self) -> list[str]:
        return list(self._collectors.keys())
