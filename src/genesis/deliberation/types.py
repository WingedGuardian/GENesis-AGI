"""Types for `deliberate()` — the chorus (Track 4, omnipresence layer)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Stakes = Literal["normal", "high"]


@dataclass(frozen=True)
class PerModel:
    """A single panel member's position, when the backend exposes it."""

    model: str
    answer: str | None = None
    stance: str | None = None  # "agree" | "dissent" | None


@dataclass(frozen=True)
class DeliberationResult:
    """The outcome of a `deliberate()` call.

    Frozen + tuple-typed to match the routing/types.py convention. ``error`` is the
    single failure channel — ``deliberate()`` never raises; on failure ``answer`` is
    None and ``error`` is set.
    """

    answer: str | None
    consensus: str | None = None
    dissent: tuple[str, ...] = ()
    blind_spots: tuple[str, ...] = ()
    confidence: float | None = None
    per_model: tuple[PerModel, ...] = ()
    backend_used: str = "fusion"
    preset_used: str | None = None
    latency_s: float | None = None
    cost_usd: float = 0.0
    cost_known: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
