"""Polymarket connector — fetches active markets from Polymarket CLOB API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiohttp

from genesis.modules.prediction_markets.types import Market, MarketSource, MarketStatus

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"


class PolymarketConnector:
    """Fetches active prediction markets from Polymarket's public CLOB API.

    Uses the public read-only endpoints (no auth required).
    """

    name = "polymarket"

    async def fetch_markets(self, *, limit: int = 50) -> list[Market]:
        """Fetch active markets from Polymarket CLOB."""
        markets: list[Market] = []
        now_iso = datetime.now(UTC).isoformat()

        try:
            async with aiohttp.ClientSession() as session:
                # Polymarket CLOB API: GET /markets returns active markets
                url = f"{CLOB_BASE}/markets?limit={limit}&active=true"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        logger.warning("Polymarket API returned %d", resp.status)
                        return []
                    data = await resp.json()

            # data is a list of market objects
            if not isinstance(data, list):
                data = data.get("data", []) if isinstance(data, dict) else []

            for item in data:
                try:
                    # Polymarket market fields vary; extract what's available
                    condition_id = item.get("condition_id", "")
                    question = item.get("question", "")
                    description = item.get("description", "")

                    # Tokens contain outcome prices
                    tokens = item.get("tokens", [])
                    # First token is typically "Yes" outcome
                    yes_price = 0.5
                    if tokens and isinstance(tokens, list):
                        yes_token = tokens[0]
                        yes_price = float(yes_token.get("price", 0.5))

                    market_slug = item.get("market_slug", condition_id)
                    volume = float(item.get("volume", 0) or 0)
                    liquidity = float(item.get("liquidity", 0) or 0)

                    end_date = item.get("end_date_iso", "")
                    if not end_date:
                        end_date = item.get("end_date", "")

                    # Determine status
                    active = item.get("active", True)
                    closed = item.get("closed", False)
                    if closed:
                        status = MarketStatus.CLOSED
                    elif active:
                        status = MarketStatus.OPEN
                    else:
                        status = MarketStatus.CLOSED

                    categories = []
                    if item.get("category"):
                        categories = [item["category"]]
                    elif item.get("tags"):
                        categories = item["tags"] if isinstance(item["tags"], list) else [item["tags"]]

                    markets.append(Market(
                        id=f"poly_{condition_id}",
                        source=MarketSource.POLYMARKET,
                        title=question,
                        description=description[:500] if description else "",
                        url=f"https://polymarket.com/event/{market_slug}",
                        current_price=yes_price,
                        volume=volume,
                        liquidity=liquidity,
                        close_date=end_date,
                        status=status,
                        categories=categories,
                        metadata={
                            "condition_id": condition_id,
                            "market_slug": market_slug,
                            "token_count": len(tokens),
                        },
                        fetched_at=now_iso,
                    ))
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug("Skipping malformed Polymarket item: %s", e)
                    continue

        except aiohttp.ClientError:
            logger.warning("Polymarket API request failed", exc_info=True)
        except Exception:
            logger.warning("Polymarket connector error", exc_info=True)

        logger.info("Polymarket: fetched %d markets", len(markets))
        return markets
