"""CryptoOpsModule — CapabilityModule implementation for crypto token operations."""

from __future__ import annotations

import json
import logging
from typing import Any

from genesis.modules.crypto_ops.monitor import PositionMonitor
from genesis.modules.crypto_ops.narrative import NarrativeDetector
from genesis.modules.crypto_ops.tracker import CryptoOutcomeTracker

logger = logging.getLogger(__name__)

GENERALIZE_PROMPT = """You are evaluating a crypto token launch outcome for generalizable lessons.

Launch details:
- Narrative: {narrative}
- Chain: {chain}
- Token: {token}
- P&L: {pnl:+.1%}
- Narrative prediction accurate: {narrative_accurate}
- Timing: {timing}

Is there a process/methodology lesson here that would help reason better in ANY domain?

Rules:
- Market-specific patterns (e.g., "Solana memes pump on weekends") are NOT generalizable
- Source/data quality findings ARE generalizable
- Research methodology improvements ARE generalizable
- Timing calibration insights ARE generalizable
- Narrative detection methodology improvements ARE generalizable
- Random outcomes are NEVER generalizable
- When in doubt, DO NOT promote.

If generalizable, respond with JSON:
{{"generalizable": true, "lesson": "<domain-agnostic observation>", \
"category": "<process|source_reliability|calibration|tool_effectiveness>"}}

If not:
{{"generalizable": false, "reason": "<brief>"}}
"""


class CryptoOpsModule:
    """Crypto Token Operations — narrative detection + deployment.

    Semi-autonomous: Genesis detects narratives, prepares launch packages,
    monitors positions. User approves each launch decision.
    """

    def __init__(
        self,
        *,
        narrative_detector: NarrativeDetector | None = None,
        position_monitor: PositionMonitor | None = None,
        tracker: CryptoOutcomeTracker | None = None,
    ) -> None:
        self._narrative_detector = narrative_detector or NarrativeDetector()
        self._position_monitor = position_monitor or PositionMonitor()
        self._tracker = tracker or CryptoOutcomeTracker()
        self._enabled = False
        self._runtime: Any = None

    @property
    def name(self) -> str:
        return "crypto_ops"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    async def register(self, runtime: Any) -> None:
        self._runtime = runtime
        logger.info("Crypto ops module registered")

    async def deregister(self) -> None:
        self._runtime = None
        logger.info("Crypto ops module deregistered")

    def get_research_profile_name(self) -> str | None:
        return "crypto-ops"

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        """Process a surfaced narrative opportunity.

        Expects opportunity with 'signals' (pipeline output) and 'router'.
        Returns a launch proposal for user approval.
        """
        signals = opportunity.get("signals", [])
        router = opportunity.get("router")

        if not signals:
            return None

        # Detect narratives from signals
        narratives = await self._narrative_detector.detect(signals, router=router)
        if not narratives:
            return None

        # Return the strongest narrative as an opportunity
        best = narratives[0]  # Already sorted by momentum
        return {
            "type": "crypto_narrative",
            "narrative": {
                "id": best.id,
                "name": best.name,
                "description": best.description,
                "momentum": best.momentum_score,
                "status": best.status,
                "signals": best.signals,
            },
            "suggested_action": "Prepare launch package for this narrative",
            "requires_approval": True,
        }

    async def record_outcome(self, outcome: dict) -> None:
        """Record a launch outcome in isolated tracking."""
        self._tracker.record_launch(
            launch_id=outcome.get("launch_id", ""),
            narrative_name=outcome.get("narrative_name", ""),
            chain=outcome.get("chain", ""),
            token_name=outcome.get("token_name", ""),
            invested=outcome.get("invested", 0.0),
        )

    async def extract_generalizable(
        self,
        outcome: dict,
        *,
        router: Any = None,
    ) -> list[dict] | None:
        """Extract generalizable lessons from a launch outcome."""
        if router is None:
            return None

        prompt = GENERALIZE_PROMPT.format(
            narrative=outcome.get("narrative_name", "Unknown"),
            chain=outcome.get("chain", "unknown"),
            token=outcome.get("token_name", "unknown"),
            pnl=outcome.get("pnl_pct", 0.0),
            narrative_accurate=outcome.get("narrative_accurate", "unknown"),
            timing=outcome.get("timing", "unknown"),
        )

        try:
            response = await router.route(prompt, tier="free")
            result = json.loads(response)
        except Exception:
            logger.warning("Generalization extraction failed", exc_info=True)
            return None

        if not result.get("generalizable", False):
            return None

        return [{
            "source": "module:crypto_ops",
            "lesson": result.get("lesson", ""),
            "category": result.get("category", "process"),
        }]

    def configurable_fields(self) -> list[dict]:
        """Return user-editable configuration fields."""
        mon = self._position_monitor
        return [
            {"name": "volume_drop_threshold", "label": "Volume Drop Threshold", "type": "float",
             "value": mon._volume_drop_threshold, "description": "Volume drop % that triggers exit signal"},
            {"name": "liquidity_drop_threshold", "label": "Liquidity Drop Threshold", "type": "float",
             "value": mon._liquidity_drop_threshold, "description": "Liquidity drop % that triggers exit signal"},
            {"name": "momentum_exit_threshold", "label": "Momentum Exit Threshold", "type": "float",
             "value": mon._momentum_exit_threshold, "description": "Narrative momentum below this triggers exit"},
        ]

    def update_config(self, updates: dict) -> dict:
        """Apply configuration updates with bounds validation."""
        mon = self._position_monitor
        for key in ("volume_drop_threshold", "liquidity_drop_threshold", "momentum_exit_threshold"):
            if key in updates:
                val = float(updates[key])
                if not 0 <= val <= 1:
                    raise ValueError(f"{key} must be in [0, 1]")
                setattr(mon, f"_{key}", val)
        return {f["name"]: f["value"] for f in self.configurable_fields()}
