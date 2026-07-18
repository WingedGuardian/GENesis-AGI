"""Canonical signal-line formatting for reflection prompts.

Every reflection depth must present live tick signals in ONE format. Two
divergent formatters (``perception/context.py`` rich lines vs
``cc/reflection_bridge/_prompts.py`` compact comma-join) meant light and deep
reflections saw differently-shaped signal blocks — a deep cycle could not
recognize the signal a light cycle cited, feeding the assert/debunk loop.
The rich variant is canonical: it carries source, thresholds, baseline
ground truth, and persistence, all of which the model needs to cite a
signal honestly. Both legacy call sites are thin delegates onto this module.
"""

from __future__ import annotations

from collections.abc import Iterable

from genesis.awareness.types import SignalReading

# Awareness loop tick interval — used only for the human-readable
# "persistent ~Nh" annotation.
_TICK_INTERVAL_MINUTES = 5


def format_signal_line(s: SignalReading, *, unchanged_ticks: int = 0) -> str:
    """One canonical line for one signal: name, value, source, threshold
    status, baseline ground truth, persistence."""
    line = f"{s.name}: {s.value} (source={s.source})"
    if s.normal_max is not None:
        status = (
            "CRITICAL"
            if s.critical_threshold is not None and s.value >= s.critical_threshold
            else "WARNING"
            if s.warning_threshold is not None and s.value >= s.warning_threshold
            else "normal"
        )
        line += (
            f" [{status}; normal<={s.normal_max},"
            f" warn>={s.warning_threshold}, crit>={s.critical_threshold}]"
        )
    if s.baseline_note:
        line += f" -- baseline: {s.baseline_note}"
    if unchanged_ticks >= 2:
        hours = unchanged_ticks * _TICK_INTERVAL_MINUTES / 60
        line += f" (persistent ~{hours:.1f}h)"
    return line


def format_signals(
    signals: Iterable[SignalReading],
    *,
    staleness: dict[str, int] | None = None,
    excluded_signals: set[str] | None = None,
    min_value: float = 0.0,
    empty: str = "",
) -> str:
    """Newline-joined canonical lines for a signal collection.

    ``min_value`` > 0 excludes signals at or below that value (bootstrap
    placeholder filtering). ``excluded_signals`` drops names outright.
    No silent truncation: every passing signal renders.
    """
    staleness = staleness or {}
    lines = [
        format_signal_line(s, unchanged_ticks=staleness.get(s.name, 0))
        for s in signals
        if not (excluded_signals is not None and s.name in excluded_signals)
        and not (min_value > 0 and s.value <= min_value)
    ]
    return "\n".join(lines) if lines else empty
