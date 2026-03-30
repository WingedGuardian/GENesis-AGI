"""Outcome tracker for crypto token operations — isolated P&L and metrics."""

from __future__ import annotations

import logging
from collections import defaultdict

from genesis.modules.crypto_ops.types import LaunchStatus

logger = logging.getLogger(__name__)


class CryptoOutcomeTracker:
    """Tracks crypto token launch outcomes in isolation from Genesis core.

    Records:
    - P&L per token launch
    - Narrative accuracy (did the predicted wave materialize?)
    - Timing quality (early/late/on-time)
    - Win rate, average return

    None of this feeds Genesis core learning directly — only generalizable
    process lessons go through the generalization filter.
    """

    def __init__(self) -> None:
        self._launches: dict[str, dict] = {}

    @property
    def total_launches(self) -> int:
        return len(self._launches)

    def record_launch(
        self,
        launch_id: str,
        *,
        narrative_name: str = "",
        chain: str = "",
        token_name: str = "",
        invested: float = 0.0,
    ) -> None:
        """Record a new token launch."""
        self._launches[launch_id] = {
            "launch_id": launch_id,
            "narrative_name": narrative_name,
            "chain": chain,
            "token_name": token_name,
            "invested": invested,
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "narrative_accurate": None,  # True/False after review
            "timing": None,  # "early", "on-time", "late"
            "status": LaunchStatus.DEPLOYED,
        }

    def record_exit(
        self,
        launch_id: str,
        *,
        pnl: float,
        narrative_accurate: bool | None = None,
        timing: str | None = None,
    ) -> None:
        """Record exit and final P&L for a launch."""
        launch = self._launches.get(launch_id)
        if launch is None:
            logger.warning("Unknown launch %s", launch_id)
            return
        launch["pnl"] = pnl
        launch["invested"] = launch.get("invested", 0.0)
        if launch["invested"] > 0:
            launch["pnl_pct"] = pnl / launch["invested"]
        launch["status"] = LaunchStatus.EXITED
        if narrative_accurate is not None:
            launch["narrative_accurate"] = narrative_accurate
        if timing is not None:
            launch["timing"] = timing

    def win_rate(self) -> float | None:
        """Win rate across exited launches."""
        exited = [rec for rec in self._launches.values() if rec["status"] == LaunchStatus.EXITED]
        if not exited:
            return None
        wins = sum(1 for rec in exited if rec["pnl"] > 0)
        return wins / len(exited)

    def total_pnl(self) -> float:
        return sum(rec["pnl"] for rec in self._launches.values())

    def average_return(self) -> float | None:
        """Average return percentage across exited launches."""
        exited = [rec for rec in self._launches.values()
                  if rec["status"] == LaunchStatus.EXITED and rec.get("invested", 0) > 0]
        if not exited:
            return None
        return sum(rec["pnl_pct"] for rec in exited) / len(exited)

    def narrative_accuracy(self) -> float | None:
        """Percentage of launches where narrative prediction was accurate."""
        rated = [rec for rec in self._launches.values() if rec["narrative_accurate"] is not None]
        if not rated:
            return None
        accurate = sum(1 for rec in rated if rec["narrative_accurate"])
        return accurate / len(rated)

    def timing_breakdown(self) -> dict[str, int]:
        """Count of early/on-time/late launches."""
        result: dict[str, int] = defaultdict(int)
        for rec in self._launches.values():
            if rec.get("timing"):
                result[rec["timing"]] += 1
        return dict(result)

    def chain_breakdown(self) -> dict[str, dict]:
        """Per-chain performance summary."""
        chains: dict[str, list] = defaultdict(list)
        for rec in self._launches.values():
            if rec.get("chain"):
                chains[rec["chain"]].append(rec)

        result = {}
        for chain, launches in chains.items():
            exited = [rec for rec in launches if rec["status"] == LaunchStatus.EXITED]
            wins = sum(1 for rec in exited if rec["pnl"] > 0)
            result[chain] = {
                "total": len(launches),
                "exited": len(exited),
                "win_rate": wins / len(exited) if exited else None,
                "total_pnl": sum(rec["pnl"] for rec in launches),
            }
        return result

    def stats(self) -> dict:
        """Overall performance summary."""
        return {
            "total_launches": self.total_launches,
            "win_rate": self.win_rate(),
            "total_pnl": self.total_pnl(),
            "average_return": self.average_return(),
            "narrative_accuracy": self.narrative_accuracy(),
            "timing": self.timing_breakdown(),
            "chains": self.chain_breakdown(),
        }
