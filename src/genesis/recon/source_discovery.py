"""Source discovery — find new repos, tools, and blogs to monitor.

LLM-driven: prompts with current watchlist and cognitive state, then
verifies suggestions via web search. Output is surfaced as findings for
user/ego approval — never auto-adds to watchlist.

Cadence: monthly (1st), per config/recon_schedules.yaml.
Ships disabled: first run deferred until user enables.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)


class SourceDiscoveryJob:
    """Discovers new intelligence sources for the recon pipeline."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        enabled: bool = False,
    ) -> None:
        self._db = db
        self._enabled = enabled

    async def run(self) -> dict:
        """Run a discovery cycle. Returns summary dict.

        Currently a stub — ships disabled. When enabled, will:
        1. Gather current watchlist + recent cognitive state
        2. Prompt LLM (via intake call site) for new source suggestions
        3. Verify suggestions exist via web search
        4. Route verified suggestions through intake (confidence 0.4)
        """
        if not self._enabled:
            logger.info("Source discovery: disabled — skipping")
            return {"enabled": False, "suggestions": 0}

        # TODO: Implement when user enables source discovery.
        # The infrastructure is in place:
        # - IntakeSource.SOURCE_DISCOVERY (confidence 0.4)
        # - 45_intelligence_intake call site for LLM scoring
        # - Web search via genesis providers
        #
        # Implementation outline:
        # 1. Load current watchlist from config/recon_watchlist.yaml
        # 2. Load recent cognitive state (active projects, interests)
        # 3. Build prompt asking for new sources to monitor
        # 4. Route LLM call through 45_intelligence_intake
        # 5. For each suggestion, verify via web search
        # 6. Route verified suggestions through run_intake()
        logger.info("Source discovery: enabled but not yet implemented")
        return {"enabled": True, "suggestions": 0, "status": "not_implemented"}
