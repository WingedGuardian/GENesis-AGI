"""Per-step timings for one hook run, rendered as a single stderr line.

A hook's soft deadline and its hard stop (``hook_deadline.py``) bound how long
it runs, but neither says WHICH step used the time. This module records that:
the hook calls ``mark(name)`` as each step starts, and writes ``line()`` to
stderr once, at exit. Claude Code keeps an exit-0 hook's stderr in the
transcript's ``hook_success`` row and never shows it to the model (hooks docs:
"Claude never sees stderr from a hook that exits 0"), so the line costs the
session nothing and gives a later reader per-step milliseconds plus the
container's pressure stall averages for that moment.

``mark(name)`` means "starting step ``name``": each phase lasts until the next
mark, and the last one is still open when the line is built. On a cut line (the
hard stop fired) that open phase is the step that was running, named as
``running``.

The hard stop calls ``line()`` from its timer thread while the main thread may
still be marking, so ``line()`` copies its state with single C-level calls
(a list slice, ``dict()``), imports nothing, and reads pressure files with
``open(..., "rb")`` (cgroupfs lives in memory and does not wait on a disk).

STDLIB-ONLY. Shared by hooks under ``scripts/``; it reads no Genesis state.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

PHASE_LINE_PREFIX = "[hook-phases] "

#: The whole line, newline included. One phase line plus the hard stop's stack
#: sample (up to 40 frames) stays under 3 KB, and the largest hook stderr
#: observed stored whole in a transcript was 2,381 characters (2026-10-19), so a
#: line far below that is the safe side of a storage limit nobody has measured.
MAX_LINE_BYTES = 1000

#: Free-text fields (a server failure reason) are clipped to this many characters.
MAX_FIELD_CHARS = 80

#: (cgroup file, PSI line kind, window) -> output key. avg10 covers the whole
#: run, which is at most ~8.5 s; io also keeps avg60, which shows a stall that
#: was building before the run started.
_PSI_KEYS: dict[tuple[str, bytes, bytes], str] = {
    ("io", b"some", b"avg10"): "io_some10",
    ("io", b"some", b"avg60"): "io_some60",
    ("io", b"full", b"avg10"): "io_full10",
    ("io", b"full", b"avg60"): "io_full60",
    ("cpu", b"some", b"avg10"): "cpu_some10",
    ("memory", b"full", b"avg10"): "mem_full10",
}
_PSI_RESOURCES = ("io", "cpu", "memory")


def read_psi(root: str = "/sys/fs/cgroup") -> dict[str, float | None]:
    """Pressure stall averages from this cgroup namespace's root. Unreadable is None."""
    out: dict[str, float | None] = dict.fromkeys(_PSI_KEYS.values())
    for resource in _PSI_RESOURCES:
        try:
            with open(f"{root}/{resource}.pressure", "rb") as fh:
                data = fh.read(512)
        except OSError:
            continue
        for row in data.split(b"\n"):
            parts = row.split()
            if not parts:
                continue
            for part in parts[1:]:
                key, _, value = part.partition(b"=")
                name = _PSI_KEYS.get((resource, parts[0], key))
                if name is None:
                    continue
                try:
                    out[name] = float(value)
                except ValueError:
                    out[name] = None
    return out


class PhaseClock:
    """Ordered step marks for one hook process, rendered by ``line()``."""

    def __init__(
        self,
        *,
        age_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        psi_root: object = "/sys/fs/cgroup",
    ) -> None:
        self._clock = clock
        self._t0 = clock()
        self._age_s = age_s  # process age when this clock started, or None
        self._psi_root = str(psi_root)
        self._marks: list[tuple[str, float]] = []
        self._module_marks = 0
        self._fields: dict[str, object] = {}

    def mark(self, name: str) -> None:
        """Step ``name`` starts now."""
        self._marks.append((name, self._clock()))

    def set(self, **fields: object) -> None:
        """Record facts about this run (its path, why the server failed, ...)."""
        self._fields.update(fields)

    def freeze_module(self) -> None:
        """Marks so far belong to module load and survive ``reset_run``."""
        self._module_marks = len(self._marks)

    def reset_run(self) -> None:
        """Drop run marks and facts. A no-op in a hook process, which runs once;
        under pytest one imported module serves many runs."""
        del self._marks[self._module_marks :]
        self._fields.clear()

    def line(self, *, cut: bool = False, **extra: object) -> str:
        """The ``[hook-phases] {json}`` line, newline-terminated, under MAX_LINE_BYTES."""
        now = self._clock()
        marks = self._marks[:]
        fields = dict(self._fields)
        fields.update(extra)
        phases: list[list[object]] = []
        for i, (name, started) in enumerate(marks):
            ended = marks[i + 1][1] if i + 1 < len(marks) else now
            phases.append([name, round((ended - started) * 1000, 1)])
        payload: dict[str, object] = {
            "v": 1,
            "cut": cut,
            "age0_s": None if self._age_s is None else round(self._age_s, 3),
            "total_s": round((self._age_s or 0.0) + (now - self._t0), 3),
        }
        for key, value in fields.items():
            if isinstance(value, str):
                value = value[:MAX_FIELD_CHARS]
            payload[key] = value
        if cut and phases:
            payload["running"] = phases[-1][0]
            payload["running_ms"] = phases[-1][1]
        payload["psi"] = read_psi(self._psi_root)
        payload["phases"] = phases
        text = self._render(payload)
        if len(text.encode("utf-8")) > MAX_LINE_BYTES:
            payload["phases"] = self._fit(payload, phases)
            text = self._render(payload)
        if len(text.encode("utf-8")) > MAX_LINE_BYTES:
            text = self._render({"v": 1, "cut": cut, "truncated": True})
        return text

    def _fit(self, payload: dict[str, object], phases: list[list[object]]) -> list[list[object]]:
        """Keep the NEWEST phases that fit (the running step on a cut line), and
        fold the older ones into one ``["+N", ms]`` entry. One pass: each kept
        entry is sized once, so the cost is linear in the number of phases."""
        # Size the fold entry at its worst case: every phase folded, so the widest
        # count and the widest millisecond sum it can ever carry.
        total_ms = round(sum(float(p[1]) for p in phases), 1)  # type: ignore[arg-type]
        payload["phases"] = [[f"+{len(phases)}", total_ms]]
        room = MAX_LINE_BYTES - len(self._render(payload).encode("utf-8"))
        kept: list[list[object]] = []
        for entry in reversed(phases):
            cost = len(json.dumps(entry, separators=(",", ":")).encode("utf-8")) + 1  # + comma
            if cost > room:
                break
            room -= cost
            kept.append(entry)
        kept.reverse()
        result = self._folded(phases, kept)
        # The estimate is an upper bound, so this rarely runs; it guarantees the
        # line keeps content rather than falling back to the bare stub.
        while kept:
            payload["phases"] = result
            if len(self._render(payload).encode("utf-8")) <= MAX_LINE_BYTES:
                break
            kept = kept[1:]
            result = self._folded(phases, kept)
        return result

    @staticmethod
    def _folded(phases: list[list[object]], kept: list[list[object]]) -> list[list[object]]:
        folded = phases[: len(phases) - len(kept)]
        if not folded:
            return kept
        folded_ms = round(sum(float(p[1]) for p in folded), 1)  # type: ignore[arg-type]
        return [[f"+{len(folded)}", folded_ms], *kept]

    @staticmethod
    def _render(payload: dict[str, object]) -> str:
        return PHASE_LINE_PREFIX + json.dumps(payload, separators=(",", ":"), default=str) + "\n"
