"""Position monitor — tracks deployed tokens, detects exit signals."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from genesis.modules.crypto_ops.types import TokenPosition

logger = logging.getLogger(__name__)


@dataclass
class ExitSignal:
    """A signal suggesting it may be time to exit a position."""

    position_id: str
    reason: str
    severity: str = "medium"  # low, medium, high
    details: str = ""


class PositionMonitor:
    """Monitors deployed token positions for exit signals.

    Tracks: price, volume, holder count, liquidity depth, narrative health.
    Suggests exit when conditions deteriorate.
    """

    def __init__(
        self,
        *,
        volume_drop_threshold: float = 0.7,  # 70% volume drop = exit signal
        liquidity_drop_threshold: float = 0.5,  # 50% liquidity drop
        momentum_exit_threshold: float = 0.2,  # Narrative momentum below this
    ) -> None:
        self._positions: dict[str, TokenPosition] = {}
        self._volume_drop_threshold = volume_drop_threshold
        self._liquidity_drop_threshold = liquidity_drop_threshold
        self._momentum_exit_threshold = momentum_exit_threshold
        self._history: dict[str, list[dict]] = {}  # position_id -> snapshots

    @property
    def active_positions(self) -> list[TokenPosition]:
        return [p for p in self._positions.values() if not p.exited_at]

    @property
    def position_count(self) -> int:
        return len(self._positions)

    def add_position(self, position: TokenPosition) -> None:
        self._positions[position.id] = position
        self._history[position.id] = []

    def get_position(self, position_id: str) -> TokenPosition | None:
        return self._positions.get(position_id)

    def update_position(
        self,
        position_id: str,
        *,
        current_price: float | None = None,
        volume_24h: float | None = None,
        holder_count: int | None = None,
        liquidity: float | None = None,
        narrative_momentum: float | None = None,
    ) -> TokenPosition | None:
        """Update position metrics and record snapshot."""
        pos = self._positions.get(position_id)
        if pos is None:
            return None

        # Record snapshot before update
        self._history.setdefault(position_id, []).append({
            "price": pos.current_price,
            "volume": pos.volume_24h,
            "holders": pos.holder_count,
            "liquidity": pos.liquidity,
            "momentum": pos.narrative_momentum,
            "ts": pos.last_checked,
        })

        if current_price is not None:
            pos.current_price = current_price
            if pos.entry_price > 0:
                pos.current_value = (current_price / pos.entry_price) * pos.invested
                pos.pnl = pos.current_value - pos.invested
                pos.pnl_pct = pos.pnl / pos.invested if pos.invested > 0 else 0
        if volume_24h is not None:
            pos.volume_24h = volume_24h
        if holder_count is not None:
            pos.holder_count = holder_count
        if liquidity is not None:
            pos.liquidity = liquidity
        if narrative_momentum is not None:
            pos.narrative_momentum = narrative_momentum

        pos.last_checked = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        return pos

    def check_exit_signals(self, position_id: str) -> list[ExitSignal]:
        """Check a position for exit signals."""
        pos = self._positions.get(position_id)
        if pos is None or pos.exited_at:
            return []

        signals = []
        history = self._history.get(position_id, [])

        # Volume drop
        if history and history[-1].get("volume", 0) > 0:
            prev_volume = history[-1]["volume"]
            if prev_volume > 0 and pos.volume_24h / prev_volume < (1 - self._volume_drop_threshold):
                signals.append(ExitSignal(
                    position_id=position_id,
                    reason="volume_drop",
                    severity="high",
                    details=f"Volume dropped {(1 - pos.volume_24h / prev_volume):.0%} from previous check",
                ))

        # Liquidity drop
        if history and history[-1].get("liquidity", 0) > 0:
            prev_liq = history[-1]["liquidity"]
            if prev_liq > 0 and pos.liquidity / prev_liq < (1 - self._liquidity_drop_threshold):
                signals.append(ExitSignal(
                    position_id=position_id,
                    reason="liquidity_drop",
                    severity="high",
                    details=f"Liquidity dropped {(1 - pos.liquidity / prev_liq):.0%}",
                ))

        # Narrative fading
        if pos.narrative_momentum < self._momentum_exit_threshold:
            signals.append(ExitSignal(
                position_id=position_id,
                reason="narrative_fading",
                severity="medium",
                details=f"Narrative momentum at {pos.narrative_momentum:.1%}, below threshold",
            ))

        # Heavy loss
        if pos.pnl_pct < -0.5:
            signals.append(ExitSignal(
                position_id=position_id,
                reason="heavy_loss",
                severity="high",
                details=f"Position down {pos.pnl_pct:.0%}",
            ))

        return signals

    def mark_exited(self, position_id: str, exit_price: float) -> TokenPosition | None:
        """Mark a position as exited."""
        pos = self._positions.get(position_id)
        if pos is None:
            return None
        pos.exit_price = exit_price
        pos.exited_at = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if pos.entry_price > 0:
            pos.current_value = (exit_price / pos.entry_price) * pos.invested
            pos.pnl = pos.current_value - pos.invested
            pos.pnl_pct = pos.pnl / pos.invested if pos.invested > 0 else 0
        return pos
