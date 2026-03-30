"""CoinGecko collector — trending coins and market data via free API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import aiohttp

from genesis.pipeline.types import CollectorResult, ResearchSignal

logger = logging.getLogger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Free tier: 10-30 calls/min. Simple rate limit via minimum interval.
_MIN_REQUEST_INTERVAL = 3.0  # seconds between requests
_rate_lock = asyncio.Lock()
_last_request_time = 0.0


async def _rate_limited_get(session: aiohttp.ClientSession, url: str) -> dict | list | None:
    """GET with basic rate limiting for CoinGecko free tier."""
    import time

    global _last_request_time
    async with _rate_lock:
        now = time.monotonic()
        wait = _MIN_REQUEST_INTERVAL - (now - _last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_time = time.monotonic()

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:
                logger.warning("CoinGecko rate limited (429)")
                return None
            resp.raise_for_status()
            return await resp.json()
    except Exception:
        logger.warning("CoinGecko request failed: %s", url, exc_info=True)
        return None


class CoinGeckoCollector:
    """Collects trending coins and top-volume markets from CoinGecko."""

    name = "coingecko"

    def __init__(self, profile_name: str) -> None:
        self._profile_name = profile_name

    async def collect(self, queries: list[str], *, max_results: int = 20) -> CollectorResult:
        signals: list[ResearchSignal] = []
        errors: list[str] = []
        now_iso = datetime.now(UTC).isoformat()

        async with aiohttp.ClientSession() as session:
            # Trending coins
            if any("trending" in q.lower() for q in queries) or not queries:
                data = await _rate_limited_get(session, f"{COINGECKO_BASE}/search/trending")
                if data and "coins" in data:
                    for item in data["coins"][:max_results]:
                        coin = item.get("item", {})
                        content = (
                            f"Trending: {coin.get('name', '?')} ({coin.get('symbol', '?')})\n"
                            f"Market cap rank: {coin.get('market_cap_rank', '?')}\n"
                            f"Price BTC: {coin.get('price_btc') or 0:.8f}"
                        )
                        signals.append(ResearchSignal(
                            id=str(uuid.uuid4()),
                            source="coingecko",
                            profile_name=self._profile_name,
                            content=content,
                            url=f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                            collected_at=now_iso,
                            tags=["trending", coin.get("symbol", "").lower()],
                            metadata={
                                "coin_id": coin.get("id"),
                                "symbol": coin.get("symbol"),
                                "market_cap_rank": coin.get("market_cap_rank"),
                            },
                        ))
                elif data is None:
                    errors.append("CoinGecko trending endpoint failed")

            # Top by volume
            volume_queries = [q for q in queries if "volume" in q.lower() or "top" in q.lower()]
            if volume_queries or not queries:
                data = await _rate_limited_get(
                    session,
                    f"{COINGECKO_BASE}/coins/markets?vs_currency=usd&order=volume_desc&per_page={max_results}&page=1",
                )
                if isinstance(data, list):
                    for coin in data:
                        content = (
                            f"{coin.get('name', '?')} ({(coin.get('symbol') or '?').upper()})\n"
                            f"Price: ${coin.get('current_price') or 0:,.2f}\n"
                            f"24h volume: ${coin.get('total_volume') or 0:,.0f}\n"
                            f"Market cap: ${coin.get('market_cap') or 0:,.0f}\n"
                            f"24h change: {coin.get('price_change_percentage_24h') or 0:.1f}%"
                        )
                        signals.append(ResearchSignal(
                            id=str(uuid.uuid4()),
                            source="coingecko",
                            profile_name=self._profile_name,
                            content=content,
                            url=f"https://www.coingecko.com/en/coins/{coin.get('id', '')}",
                            collected_at=now_iso,
                            tags=["market_data", coin.get("symbol", "").lower()],
                            metadata={
                                "coin_id": coin.get("id"),
                                "symbol": coin.get("symbol"),
                                "current_price": coin.get("current_price"),
                                "total_volume": coin.get("total_volume"),
                                "market_cap": coin.get("market_cap"),
                                "price_change_24h_pct": coin.get("price_change_percentage_24h"),
                            },
                        ))
                elif data is None:
                    errors.append("CoinGecko markets endpoint failed")

        return CollectorResult(collector_name="coingecko", signals=signals, errors=errors)
