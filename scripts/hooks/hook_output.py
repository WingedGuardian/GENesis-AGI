"""Bounded stdout for hooks whose output reaches the model.

Claude Code PERSISTS a hook's stdout above a size threshold: the whole output
goes to a file under the session's ``tool-results/`` directory and the model
receives a ~2 KB preview. Nothing errors. The hook's contribution to that window
is simply gone, and the failure is indistinguishable from success.

MEASURED on CC 2.1.246 (2026-08-30, ~25 probe sessions; method and the literals
in ``docs/reference/cc-compatibility.md``):

* the threshold is **10,000 CHARACTERS** — 10,000 arrived inline, 10,001 was
  filed; 6,000 two-byte characters (12,044 bytes) arrived inline, so the unit is
  characters, not bytes. READ from the installed bundle as the literal ``1e4``
  on the hook-persistence path;
* it is **per hook ENTRY**, not per event — two SessionStart entries emitting
  9,000 characters each both arrived inline;
* it **moves between versions**: on 2.1.218 it sat near the high 20 Ks; the
  2.1.246 update dropped it to 10,000 and the filing rate tripled the same day.
  Whether it can move WITHOUT a version bump is UNVERIFIED.

Only SessionStart, UserPromptSubmit and UserPromptExpansion put a hook's bare
stdout in front of the model (READ from the bundle's attachment renderer, and
corroborated by the published hooks reference, which also names
``PostModelSwitch``). Every other event reaches the model only through JSON
``additionalContext`` / ``systemMessage`` — and each of those strings is run
through the SAME persistence path, which is why :func:`print_json_bounded`
exists alongside the plain-text helper.

**This module is the single home of that constant.** A hook that prints to the
model without going through here is one CC update away from silently losing its
output, and the repo has no other place where the number is written down.

Stdlib-only by contract, like ``hook_input.py``: a broken venv must never be
able to wedge a session through this path.
"""

from __future__ import annotations

import json
import sys
from typing import Any

#: The measured harness threshold. Output STRICTLY ABOVE this is persisted.
HOOK_STDOUT_CAP = 10_000

#: What a section divider costs: the emitters write "\n\n---\n\n" (7) and
#: ``print`` adds its newline. Under-counting here makes ``fits()`` slightly
#: optimistic, which the chokepoint then has to correct with a cut.
_DIVIDER_COST = 8

#: Default headroom under the cap. The cap is the cliff, not the target: a
#: hook that lands exactly on it has no room for a version that lowers it
#: slightly, and none for the closing line most emitters append.
DEFAULT_BUDGET = 9_800


class BoundedStdout:
    """A stdout writer that CANNOT exceed its budget.

    The enforcement point, deliberately, is the writer rather than each caller.
    Per-block ``fits()`` checks are a good way to degrade GRACEFULLY (emit a
    pointer instead of a cut), but they are opt-in, and a block that forgets to
    ask is exactly how output crosses the cap: in the emitter this was written
    for, 2 of 12 blocks checked. Here the check happens on the way out, so
    forgetting is no longer possible.

    What a cut costs, and how that cost is paid: a hard cut DESTROYS the tail,
    which is worse than being filed for any block synthesised in-process (the
    harness at least keeps a filed payload on disk). So the writer accumulates
    every byte it was ASKED to print in :attr:`intended`, and the caller is
    expected to write that whole text to a durable mirror and name the mirror in
    :attr:`mirror_hint` — the in-band cut marker then points at a file that
    always holds the full text. Truncation without that mirror is data loss;
    with it, it is a pointer.
    """

    def __init__(
        self,
        budget: int = DEFAULT_BUDGET,
        *,
        label: str,
        reserve: int = 0,
        mirror_hint: str = "",
        stream: Any = None,
    ) -> None:
        """
        :param budget: hard ceiling in CHARACTERS for everything written here.
        :param label: what this stream IS (e.g. a part name) — named in the marker.
        :param reserve: characters held back from :meth:`emit` for the caller's
            closing line, which is written with :meth:`emit_final`.
        :param mirror_hint: path (as text) where the full intended output lives.
        :param stream: output stream; defaults to ``sys.stdout`` at call time so
            tests can capture without patching the module.
        """
        self._budget = budget
        self._label = label
        self._reserve = max(0, reserve)
        self._mirror_hint = mirror_hint
        self._stream = stream
        self._emitted = 0
        self._intended: list[str] = []
        self._cut: tuple[str, int] | None = None

    # ── state ──────────────────────────────────────────────────────────
    @property
    def emitted_chars(self) -> int:
        """Characters actually written to the stream."""
        return self._emitted

    @property
    def intended_chars(self) -> int:
        """Characters the caller ASKED to write, cut or not.

        Matches ``len(self.intended)``: n chunks joined by n-1 newlines. The
        audit line prints this number, so an off-by-one here is a number the
        operator cannot reconcile against the mirror on disk.
        """
        return len(self.intended)

    @property
    def intended(self) -> str:
        """Everything the caller asked to write, in order, uncut."""
        return "\n".join(self._intended)

    @property
    def cut(self) -> tuple[str, int] | None:
        """``(block_label, chars_dropped)`` once a cut has happened, else None."""
        return self._cut

    @property
    def closed(self) -> bool:
        """True once the budget is spent; further :meth:`emit` calls are dropped."""
        return self._cut is not None

    @property
    def mirror_hint(self) -> str:
        return self._mirror_hint

    def set_mirror_hint(self, hint: str) -> None:
        """Name the durable mirror. Set before emitting so a cut can cite it."""
        self._mirror_hint = hint

    # ── writing ────────────────────────────────────────────────────────
    def fits(self, text: str, *, reserve: int = 0) -> bool:
        """Would ``text`` (plus a divider and ``reserve``) stay inside budget?

        For GRACEFUL degrade — swap a large block for a pointer before emitting.
        Not a safety mechanism: :meth:`emit` enforces the budget regardless.
        """
        ceiling = self._budget - self._reserve
        return self._emitted + len(text) + _DIVIDER_COST + reserve <= ceiling

    def emit(self, text: str, *, block: str = "") -> None:
        """Write ``text``, or as much of it as the budget allows.

        Always records ``text`` in :attr:`intended`, including after a cut, so
        the mirror stays complete no matter where the cut fell.
        """
        self._intended.append(text)
        if self.closed:
            return
        cost = len(text) + 1  # print() adds the newline
        ceiling = self._budget - self._reserve
        room = ceiling - self._emitted
        if cost <= room:
            self._write(text)
            return
        self._cut_here(text, block=block, room=room)

    def emit_final(self, text: str) -> None:
        """Write a closing line using the reserved headroom.

        Bypasses ``reserve`` (that is what it was reserved for) but never the
        budget, and is emitted even after a cut — the audit line reporting the
        cut is the one thing that must always land.
        """
        self._intended.append(text)
        room = self._budget - self._emitted
        if room <= 0:
            return
        self._write(text if len(text) + 1 <= room else text[: max(0, room - 1)])

    # ── internals ──────────────────────────────────────────────────────
    def _write(self, text: str) -> None:
        stream = self._stream if self._stream is not None else sys.stdout
        try:
            print(text, file=stream)
            stream.flush()
        except OSError:
            # A closed or broken stdout must not raise out of here. The caller's
            # crash handler REPORTS through this same method, and its `finally`
            # closes out through it too — so an unguarded write turns one broken
            # pipe into a double fault where the recovery path re-triggers the
            # fault and the exception escapes from `finally`, killing the hook
            # with a traceback instead of degrading. Silence is correct only
            # because there is nowhere left to say it: the stream is gone.
            pass
        self._emitted += len(text) + 1

    def _cut_marker(self, block: str) -> str:
        where = f" — full text: {self._mirror_hint}" if self._mirror_hint else ""
        named = f" at '{block}'" if block else ""
        return (
            f"\n_[ctx {self._label}: CUT{named} — the harness files any hook output "
            f"over {HOOK_STDOUT_CAP} chars behind a ~2 KB preview, so the rest was "
            f"withheld here rather than risking the whole part{where}]_"
        )

    def _cut_here(self, text: str, *, block: str, room: int) -> None:
        """Emit what fits plus a loud marker, then close the stream."""
        marker = self._cut_marker(block)
        # Both writes cost their length PLUS the newline print() adds, so the
        # room for content is what is left after the marker and BOTH newlines.
        content_room = room - len(marker) - 2
        kept = ""
        if content_room > 0:
            kept = text[:content_room]
            self._write(kept)
            self._write(marker)
        else:
            # No room for both. The MARKER wins — a silent stop is the failure
            # this class exists to prevent — but it must not overshoot either,
            # so it is itself trimmed to what is left.
            room_now = (self._budget - self._reserve) - self._emitted
            if room_now > 1:
                self._write(marker[: room_now - 1])
        self._cut = (block or self._label, len(text) - len(kept))


def print_bounded(
    text: str,
    *,
    label: str,
    budget: int = DEFAULT_BUDGET,
    mirror_hint: str = "",
) -> BoundedStdout:
    """One-shot bounded print for a hook that emits a single blob.

    Returns the writer so the caller can inspect :attr:`BoundedStdout.cut`.
    """
    out = BoundedStdout(budget, label=label, mirror_hint=mirror_hint)
    out.emit(text, block=label)
    return out


def print_json_bounded(
    payload: dict[str, Any],
    *,
    text_keys: tuple[str, ...] = (),
    budget: int = DEFAULT_BUDGET,
    stream: Any = None,
) -> bool:
    """Print a hook's JSON payload, trimming only its free-text fields.

    The ENVELOPE is never sacrificed. A hook's JSON carries both a decision
    (``permissionDecision``, ``decision``, ``hookEventName``) and free text
    explaining it; if the serialised payload crossed the cap and the harness
    persisted it, a consumer would get a preview instead of parseable JSON and
    the DECISION would be lost, not merely shortened. So oversize is paid for
    out of the named free-text fields — longest first — and the envelope always
    survives.

    :param text_keys: dotted paths to trimmable strings, e.g.
        ``("hookSpecificOutput.additionalContext",)``.
    :returns: True if the emitted payload is WITHIN budget. False means it was
        emitted anyway and may be persisted — the caller's decision could be
        withheld from the model. False is reachable when the envelope alone
        exceeds the budget, when no ``text_keys`` are trimmable, or when a
        named path is absent or not a string; a warning naming the reason goes
        to stderr in every such case. Callers that need the guarantee must
        check this, because "something was trimmed" is not the same claim.
    """
    stream = stream if stream is not None else sys.stdout

    def _get(path: str) -> str | None:
        node: Any = payload
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None

    def _set(path: str, value: str) -> None:
        node: Any = payload
        parts = path.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value

    note = f"\n[truncated — over the {HOOK_STDOUT_CAP}-char hook output cap]"
    # Bounded loop rather than one arithmetic pass: a trimmed field is
    # RE-SERIALISED, and JSON escaping means the serialised length is not the
    # Python length (the note's newline alone costs two characters once
    # encoded). Converging on the measured size is correct where computing it
    # is fiddly and wrong-by-one silently ships an oversize payload.
    for _ in range(4 * max(1, len(text_keys))):
        blob = json.dumps(payload)
        if len(blob) <= budget:
            break
        over = len(blob) - budget
        candidates = [(len(v), k) for k in text_keys if (v := _get(k)) is not None]
        if not candidates:
            break  # nothing trimmable — emit as-is rather than mangling a decision
        _, key = max(candidates)
        current = _get(key) or ""
        # Never stack notes when a second pass is needed.
        base = current[: -len(note)] if current.endswith(note) else current
        keep = max(0, len(base) - over - len(note) - 8)
        _set(key, base[:keep] + note)
    blob = json.dumps(payload)
    fits = len(blob) <= budget
    if not fits:
        # Emit ANYWAY: a decision the consumer never receives is worse than one
        # that may be previewed. But never claim success — an unreported
        # over-budget emit is exactly the silent loss this module exists for.
        print(
            f"[hook_output] payload is {len(blob)} chars against a {budget} budget and "
            "could not be trimmed further (no trimmable text_keys, or the envelope "
            "alone exceeds it) — the harness may persist it and withhold the decision.",
            file=sys.stderr,
        )
    print(blob, file=stream)
    stream.flush()
    return fits
