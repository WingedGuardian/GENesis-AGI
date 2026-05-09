"""ToolProvider adapter for TinyFish Agent — NL browser automation.

Runs a goal-based browser automation and returns structured results.
Paid: $0.015 per agent step. 500 one-time free credits on signup.
"""

from __future__ import annotations

import logging
import time

from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

logger = logging.getLogger(__name__)

COST_PER_STEP_USD = 0.015


class TinyFishAgentAdapter:
    """TinyFish Agent — NL goal-based web automation with structured output."""

    name = "tinyfish_agent"
    capability = ProviderCapability(
        content_types=("web_page", "structured_data"),
        categories=(ProviderCategory.WEB, ProviderCategory.EXTRACTION),
        cost_tier=CostTier.MODERATE,
        description="TinyFish — NL browser automation, $0.015/step",
    )

    async def check_health(self) -> ProviderStatus:
        try:
            from genesis.providers.tinyfish_client import _get_key

            _get_key()
            return ProviderStatus.AVAILABLE
        except ValueError:
            return ProviderStatus.UNAVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        """Run agent automation via TinyFish.

        Request keys:
            url (str): Target URL (required).
            goal (str): NL goal description (required).
            output_schema (dict): JSON Schema for structured output (optional).
            browser_profile (str): "stealth" or "lite" (default "stealth").
            max_steps (int): Max steps, 1-500 (default 100).
        """
        start = time.monotonic()

        url = request.get("url", "")
        goal = request.get("goal", "")
        if not url or not goal:
            return ProviderResult(
                success=False,
                error="'url' and 'goal' are required",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        try:
            from genesis.providers import tinyfish_client

            response = await tinyfish_client.agent_run(
                url=url,
                goal=goal,
                output_schema=request.get("output_schema"),
                browser_profile=request.get("browser_profile", "stealth"),
                max_steps=request.get("max_steps", 100),
            )

            num_steps = response.get("num_of_steps", 0)
            cost_usd = round(num_steps * COST_PER_STEP_USD, 4)
            response["cost_usd"] = cost_usd

            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=response.get("status") == "COMPLETED",
                data=response,
                latency_ms=latency,
                provider_name=self.name,
            )
        except ValueError as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )
        except Exception as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error("TinyFish agent failed", exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )
