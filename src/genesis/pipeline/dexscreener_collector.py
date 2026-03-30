"""DexScreener collector — DEX pair data, new pairs, and trending tokens."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

import aiohttp

from genesis.pipeline.types import CollectorResult, ResearchSignal

logger = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com"


class DexScreenerCollector:
    """Collects DEX pair data from DexScreener free API.

    Searches for pairs matching query terms. Returns price, volume,
    liquidity, pair age, and chain information as research signals.
    """

    name = "dexscreener"

    def __init__(self, profile_name: str) -> None:
        self._profile_name = profile_name

    async def collect(self, queries: list[str], *, max_results: int = 20) -> CollectorResult:
        signals: list[ResearchSignal] = []
        errors: list[str] = []
        now_iso = datetime.now(UTC).isoformat()
        seen_addresses: set[str] = set()  # dedup across queries

        async with aiohttp.ClientSession() as session:
            for query in queries:
                try:
                    url = f"{DEXSCREENER_BASE}/latest/dex/search?q={quote(query)}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 429:
                            errors.append(f"DexScreener rate limited for query '{query}'")
                            continue
                        resp.raise_for_status()
                        data = await resp.json()

                    pairs = data.get("pairs") or []
                    for pair in pairs[:max_results]:
                        pair_address = pair.get("pairAddress", "")
                        if pair_address in seen_addresses:
                            continue
                        seen_addresses.add(pair_address)

                        base = pair.get("baseToken", {})
                        chain_id = pair.get("chainId", "unknown")
                        price_usd = pair.get("priceUsd") or "0"
                        volume_24h = pair.get("volume", {}).get("h24") or 0
                        liquidity_usd = pair.get("liquidity", {}).get("usd") or 0
                        price_change = pair.get("priceChange") or {}
                        pair_created = pair.get("pairCreatedAt")

                        try:
                            price_float = float(price_usd)
                        except (ValueError, TypeError):
                            price_float = 0.0

                        content = (
                            f"{base.get('name', '?')} ({base.get('symbol', '?')}) on {chain_id}\n"
                            f"Price: ${price_float:,.6f}\n"
                            f"24h volume: ${volume_24h:,.0f}\n"
                            f"Liquidity: ${liquidity_usd:,.0f}\n"
                            f"24h change: {price_change.get('h24') or 0:.1f}%\n"
                            f"DEX: {pair.get('dexId', '?')}"
                        )

                        signals.append(ResearchSignal(
                            id=str(uuid.uuid4()),
                            source="dexscreener",
                            profile_name=self._profile_name,
                            content=content,
                            url=pair.get("url", f"https://dexscreener.com/{chain_id}/{pair_address}"),
                            collected_at=now_iso,
                            tags=[chain_id, base.get("symbol", "").lower(), query],
                            metadata={
                                "chain": chain_id,
                                "pair_address": pair_address,
                                "base_token": base.get("address"),
                                "base_symbol": base.get("symbol"),
                                "price_usd": price_float,
                                "volume_24h": volume_24h,
                                "liquidity_usd": liquidity_usd,
                                "price_change_24h": price_change.get("h24") or 0,
                                "dex": pair.get("dexId"),
                                "pair_created_at": pair_created,
                            },
                        ))

                except Exception as e:
                    errors.append(f"DexScreener query '{query}' failed: {e}")
                    logger.warning("DexScreener query '%s' failed", query, exc_info=True)

        return CollectorResult(collector_name="dexscreener", signals=signals, errors=errors)
