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


# What each signal's movement means, for the Light change table (#4371). The
# table states facts; whether a change matters stays the model's call.
# Counters reset or advance between Light runs by construction.
_COUNTER_SIGNALS = frozenset(
    {
        "conversations_since_reflection",
        "light_count_since_deep",
        "micro_count_since_light",
        "time_since_last_strategic",
    }
)
# 0 when healthy, above 0 while something is wrong: 0 to non-zero starts a
# condition, the reverse clears it.
_ALARM_SIGNALS = frozenset(
    {
        "autonomy_activity",
        "cc_version_changed",
        "critical_failure",
        "guardian_activity",
        "scheduled_job_health",
        "scheduler_liveness",
        "sentinel_activity",
        "software_error_spike",
        "stale_browser_processes",
        "stale_pending_items",
        "surplus_activity",
        "task_completion_quality",
    }
)
# Alarms whose collector emits a few fixed levels (stable, corrections
# accumulating, regression; investigating, escalated). Getting worse is a new
# condition for these. Every other alarm is graded (an age, a ratio, a count
# over a ceiling) and can creep up run after run, so a rise there is a
# condition only when it crosses a threshold the collector sets; otherwise it
# is listed for the model to judge. Name every fixed-level alarm collector
# here: one left out has its worsening left to the model.
_LEVEL_ALARM_SIGNALS = frozenset(
    {
        "autonomy_activity",
        "critical_failure",
        "guardian_activity",
        "scheduler_liveness",
        "sentinel_activity",
        "surplus_activity",
    }
)
# Left out of the Light reflection's signal list, its change table and the
# condition check (stored context such as an earlier observation can still
# mention it). The flag toggles on a version change
# or an available update, in about a quarter of runs (MEASURED 2026-11-11, 9-11
# of 40), and code settles it completely, so there is nothing for Light to
# think about; shown, it led 7 of 17 drift-only summaries a replay flagged as
# changed (#4371).
LIGHT_HIDDEN_SIGNALS = frozenset({"genesis_version_changed"})


def _band(s: SignalReading, value: float) -> str | None:
    """The threshold band ``value`` falls in, using ``s``'s current thresholds."""
    if s.normal_max is None:
        return None
    if s.critical_threshold is not None and value >= s.critical_threshold:
        return "CRITICAL"
    if s.warning_threshold is not None and value >= s.warning_threshold:
        return "WARNING"
    return "normal"


def _tier(s: SignalReading, value: float) -> str | None:
    """Like ``_band``, for a reading that sets warning or critical thresholds
    without a normal ceiling. None when it sets neither."""
    if s.warning_threshold is None and s.critical_threshold is None:
        return None
    if s.critical_threshold is not None and value >= s.critical_threshold:
        return "CRITICAL"
    if s.warning_threshold is not None and value >= s.warning_threshold:
        return "WARNING"
    return "below warning"


def _signal_changes(
    previous: dict[str, float | None],
    current: Iterable[SignalReading],
) -> tuple[list[tuple[str, bool]], int]:
    """One ``(line, is_condition_change)`` per signal that moved, appeared or
    went away, and the count of unchanged signals.

    A condition change is what code can settle without judgment: an alarm that
    started or cleared, a fixed-level alarm that rose
    (``_LEVEL_ALARM_SIGNALS``), an active alarm that crossed one of its own
    thresholds, a value that crossed into another threshold
    band, or a signal present in only one of the two runs. Bands exist only for
    signals whose collector sets thresholds (``container_memory_pct`` today);
    every other non-alarm value is the model's to judge, however far it moves.
    A ``previous`` value of None means that run's reading failed: what the
    signal was then is unknown, so a good reading now counts as a condition
    change. ``LIGHT_HIDDEN_SIGNALS`` are skipped on both sides.
    """
    lines: list[tuple[str, bool]] = []
    unchanged = 0
    seen: set[str] = set()
    for s in sorted(current, key=lambda r: r.name):
        if s.name in LIGHT_HIDDEN_SIGNALS:
            continue
        seen.add(s.name)
        if s.failed:
            # A collector's stub reading says nothing about the signal: never
            # compare it, or a failed alarm collector would read as "cleared".
            lines.append((f"{s.name}: (reading failed this run)", False))
            continue
        if s.name not in previous:
            lines.append((f"{s.name}: (not in the previous run) → {s.value}", True))
            continue
        before = previous[s.name]
        if before is None:
            lines.append((f"{s.name}: (reading failed in the previous run) → {s.value}", True))
            continue
        if abs(s.value - before) < 1e-9:
            unchanged += 1
            continue
        condition = False
        if s.name in _COUNTER_SIGNALS:
            note = "counter; moves every run"
        elif s.name in _ALARM_SIGNALS:
            if before == 0:
                note, condition = "alarm STARTED", True
            elif s.value == 0:
                note, condition = "alarm CLEARED", True
            elif s.value > before and s.name in _LEVEL_ALARM_SIGNALS:
                note, condition = "alarm level ROSE", True
            elif _tier(s, before) != _tier(s, s.value):
                note, condition = f"alarm still active; {_tier(s, before)} → {_tier(s, s.value)}", True
            elif s.value > before:
                note = "alarm still active; value rose"
            else:
                note = "alarm still active; level eased"
        else:
            then, now = _band(s, before), _band(s, s.value)
            if then is not None and then != now:
                note, condition = f"band {then} → {now}", True
            elif now is not None:
                note = f"still {now}"
            else:
                note = f"{s.value - before:+.3g}"
        lines.append((f"{s.name}: {before} → {s.value} ({note})", condition))
    for name in sorted(set(previous) - seen - LIGHT_HIDDEN_SIGNALS):
        was = "(reading failed)" if previous[name] is None else previous[name]
        lines.append((f"{name}: {was} → (not in this run)", True))
    return lines, unchanged


def light_signal_changes(
    previous: dict[str, float | None],
    current: Iterable[SignalReading],
) -> str:
    """The signal changes between the previous Light finding's tick and now.

    ``previous`` maps signal name to value as stored for that tick (None where
    that run's reading failed). One line per
    signal whose value moved, appeared or went away, each tagged with what kind
    of signal it is; unchanged signals are counted, not listed. Facts only: the
    table never says whether a change is material.
    """
    lines, unchanged = _signal_changes(previous, current)
    return "\n".join([line for line, _ in lines] + [f"{unchanged} other signal(s) unchanged."])


def light_condition_changes(
    previous: dict[str, float | None],
    current: Iterable[SignalReading],
) -> list[str]:
    """The change-table lines that are condition changes (``_signal_changes``).
    Empty when only drift separates the two runs."""
    return [line for line, condition in _signal_changes(previous, current)[0] if condition]


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
