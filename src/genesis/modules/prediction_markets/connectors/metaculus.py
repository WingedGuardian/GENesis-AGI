"""Metaculus connector — fetches open questions from Metaculus public API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiohttp

from genesis.modules.prediction_markets.types import Market, MarketSource, MarketStatus

logger = logging.getLogger(__name__)

METACULUS_BASE = "https://www.metaculus.com/api2"


class MetaculusConnector:
    """Fetches open prediction questions from Metaculus public API.

    Uses the public read-only API (no auth required for basic queries).
    """

    name = "metaculus"

    async def fetch_markets(self, *, limit: int = 50) -> list[Market]:
        """Fetch open questions from Metaculus."""
        markets: list[Market] = []
        now_iso = datetime.now(UTC).isoformat()

        try:
            async with aiohttp.ClientSession() as session:
                url = (
                    f"{METACULUS_BASE}/questions/"
                    f"?status=open&type=forecast&limit={limit}"
                    f"&order_by=-activity"
                )
                headers = {"Accept": "application/json"}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        logger.warning("Metaculus API returned %d", resp.status)
                        return []
                    data = await resp.json()

            results = data.get("results", []) if isinstance(data, dict) else []

            for item in results:
                try:
                    q_id = item.get("id", "")
                    title = item.get("title", "")
                    description = item.get("description", "")

                    # Community prediction is the "market price" equivalent
                    prediction = item.get("community_prediction", {})
                    if isinstance(prediction, dict):
                        community_prob = prediction.get("full", {}).get("q2", 0.5)
                    elif isinstance(prediction, (int, float)):
                        community_prob = float(prediction)
                    else:
                        community_prob = 0.5

                    # Metaculus uses prediction count as a volume proxy
                    num_predictions = item.get("number_of_predictions", 0) or 0

                    resolve_time = item.get("resolve_time", "")
                    close_time = item.get("close_time", "")

                    # Categories from tags
                    categories = []
                    if item.get("categories"):
                        categories = [
                            c.get("name", "") if isinstance(c, dict) else str(c)
                            for c in item["categories"]
                        ]

                    slug = item.get("url", f"/questions/{q_id}/")

                    markets.append(Market(
                        id=f"meta_{q_id}",
                        source=MarketSource.METACULUS,
                        title=title,
                        description=description[:500] if description else "",
                        url=f"https://www.metaculus.com{slug}" if slug.startswith("/") else slug,
                        current_price=float(community_prob),
                        volume=float(num_predictions),  # prediction count as volume proxy
                        liquidity=0.0,  # Metaculus doesn't have financial liquidity
                        close_date=close_time,
                        resolution_date=resolve_time,
                        status=MarketStatus.OPEN,
                        categories=categories,
                        metadata={
                            "metaculus_id": q_id,
                            "num_predictions": num_predictions,
                            "author_id": item.get("author", ""),
                        },
                        fetched_at=now_iso,
                    ))
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug("Skipping malformed Metaculus item: %s", e)
                    continue

        except aiohttp.ClientError:
            logger.warning("Metaculus API request failed", exc_info=True)
        except Exception:
            logger.warning("Metaculus connector error", exc_info=True)

        logger.info("Metaculus: fetched %d questions", len(markets))
        return markets
