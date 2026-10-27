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
import re
import sys
from typing import Any

#: The measured harness threshold. Output STRICTLY ABOVE this is persisted.
HOOK_STDOUT_CAP = 10_000

#: The events whose BARE STDOUT the model reads, per the module docstring above.
#: Lives here, next to the cap, because this module is the single home of the
#: contract and the gate that enforces it must DERIVE the set rather than restate
#: it -- an allowlist whose whole guarantee is "a newly wired model-facing hook
#: fails by construction" cannot be built on a hand-maintained copy of the list.
#:
#: ``PostModelSwitch`` is deliberately ABSENT. The published hooks reference names
#: it alongside these three; the bundle read that produced this list does not, and
#: nothing in .claude/settings.json is wired to it. It is excluded on the weaker
#: evidence rather than included on the stronger, because a wrong entry here makes
#: the gate demand exemptions for hooks that cannot reach the model. If a hook is
#: ever wired to it, add it here and the gate picks it up with no other change.
BARE_STDOUT_EVENTS = ("SessionStart", "UserPromptSubmit", "UserPromptExpansion")

#: What a section divider costs: the emitters write "\n\n---\n\n" (7) and
#: ``print`` adds its newline. Under-counting here makes ``fits()`` slightly
#: optimistic, which the chokepoint then has to correct with a cut.
_DIVIDER_COST = 8

#: The newline ``print`` appends. It reaches the stream, so the harness counts
#: it and so must every size check here.
_NEWLINE_COST = 1


#: Any surrogate CODE POINT. A Python ``str`` can hold one unpaired — for
#: example after ``json.loads('"\\ud800"')``, which is how hook stdin is parsed,
#: or a filename decoded with ``surrogateescape`` — and no strict encoder will
#: write it: ``print`` to a UTF-8 stdout raises ``UnicodeEncodeError``.
_SURROGATE = re.compile("[\ud800-\udfff]")

#: What one surrogate code point costs once :func:`printable` has escaped it:
#: the six characters of ``\\udXXX``.
_SURROGATE_ESCAPE_COST = 6


def printable(text: str) -> str:
    """``text`` exactly as the writers here emit it: surrogates escaped.

    Every surrogate code point becomes the six-character escape ``\\udXXX``,
    the spelling ``json.dumps`` already gives the same character, so the
    plain-text writers and :func:`print_json_bounded` show it identically. A
    replacement character would hide which code point was there; an escape is
    ASCII, so it encodes on any stream, and it keeps the information.

    One function, used by the measurement AND the write, so that what is billed
    is what is written. Measuring a cleaned string while writing the raw one (or
    the reverse) is the unit-mixing defect this module keeps having to remove.
    Text without surrogates is returned unchanged, the same object.

    UTF-8 with ``backslashreplace`` is the escape: surrogates are the only code
    points UTF-8 cannot encode, so it replaces exactly those, each on its own
    (a high and a low surrogate stored as two code points are NOT joined into
    one character), and it runs in C — MEASURED about 30x faster than a regex
    substitution on surrogate-dense text.
    """
    if text.isascii() or _SURROGATE.search(text) is None:
        return text
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 CODE UNITS — the unit the harness counts.

    The unit primitive, exported so that sizing done OUTSIDE this module (a
    sub-budget such as the charter block's ceiling) can be measured in the same
    unit the writer enforces. A sub-budget checked with ``len`` against a parent
    budget enforced in code units is not a smaller budget — it is a budget that
    silently stops bounding anything once astral text arrives.

    Measured on :func:`printable` ``(text)``, the string the writers actually
    emit, so a lone surrogate costs its six-character escape. A strict
    ``encode`` of the raw text raised on one instead (#3237).
    """
    return len(printable(text).encode("utf-16-le")) // 2


def emit_cost(text: str) -> int:
    """What writing ``text`` actually costs the budget.

    One function so no size decision can drift from another. Three call sites
    computed this independently and one of them — :meth:`BoundedStdout.fits` —
    left out the newline, which made the graceful-degrade check approve blocks
    the enforcing write then destroyed.

    Billed in UTF-16 CODE UNITS, because that is what the harness's own length
    check compares — not codepoints. An astral character (emoji, rarer CJK) is
    ONE codepoint but TWO code units, so `len()` under-bills it by half and a
    part could cross the real cap while this reported "within budget" — a silent
    filing, which is the exact bug this module exists to prevent. The probe that
    established "the unit is characters, not bytes" used a BMP character, which
    is one code unit either way and so could not distinguish the two: that
    measurement was under-determined for the conclusion drawn from it.

    No behavioural change for all-BMP text, which every injected file is today
    (MEASURED: 0 astral characters across SOUL/STEERING/USER/CONVERSATION/EK) —
    but USER.md is hand-edited and essential_knowledge.md is LLM-regenerated.
    """
    return utf16_len(text) + _NEWLINE_COST


def clip_to_cost(text: str, max_units: int) -> str:
    """Longest prefix of ``text`` costing at most ``max_units`` UTF-16 units.

    THE COMPANION TO :func:`emit_cost`, and the two must always be used
    together: billing in UTF-16 code units while slicing by CODEPOINTS is a
    silent 2x overrun for astral characters (emoji, rarer CJK). The writer keeps
    N characters believing they cost N, and they cost 2N.

    MEASURED: a part of astral text emitted 19,384 units against a 10,000 cap
    while the writer reported itself within budget — the harness then files the
    whole part, which is the exact silent context loss this module exists to
    prevent, and the cut marker naming the mirror is lost along with it. Found
    by a cross-model reviewer after the unit fix landed: correcting the
    accounting without correcting the slicing left the two disagreeing, which is
    worse than having them consistently wrong.

    Counts whole characters, so it can never split a surrogate pair. A lone
    surrogate costs what :func:`printable` writes for it (six units), the same
    unit :func:`utf16_len` bills: a clip that counted it as one would let a
    caller's ``clip_to_cost(x, cap)`` emit six times its cap.
    """
    if max_units <= 0:
        return ""
    total = 0
    for i, ch in enumerate(text):
        cp = ord(ch)
        if cp > 0xFFFF:
            cost = 2
        elif 0xD800 <= cp <= 0xDFFF:
            cost = _SURROGATE_ESCAPE_COST
        else:
            cost = 1
        if total + cost > max_units:
            return text[:i]
        total += cost
    return text


def clip_final_notice(text: str, *, emitted_units: int) -> str | None:
    """Size a hard-stop notice from the stream's locked UTF-16 count.

    The deadline writer may add a leading and trailing newline. Keep the
    existing three-unit margin, including one spare unit, under the harness
    cap. Unlike BoundedStdout's logical counter, this count includes writes
    whose accounting was interrupted by the timer. Call under the stream lock.
    """
    room = HOOK_STDOUT_CAP - emitted_units - 3
    if room <= 0:
        return None
    return clip_to_cost(text, room)


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
        :param budget: hard ceiling for everything written here, in the unit
            :func:`emit_cost` bills — UTF-16 CODE UNITS, which is what the
            harness compares. Every size decision in this class must use that
            function (or its slicing companion :func:`clip_to_cost`) rather than
            ``len``, or the two disagree on astral text.
        :param label: what this stream IS (e.g. a part name) — named in the marker.
        :param reserve: code units held back from :meth:`emit` for the caller's
            closing line, which is written with :meth:`emit_final`.
        :param mirror_hint: path (as text) where the full intended output lives.
        :param stream: output stream; defaults to ``sys.stdout`` at call time so
            tests can capture without patching the module.
        """
        self._budget = budget
        # Both are written inside the cut marker, so they are made printable
        # here, once, and every size decision below sees the written form.
        self._label = printable(label)
        self._reserve = max(0, reserve)
        self._mirror_hint = printable(mirror_hint)
        self._stream = stream
        self._emitted = 0
        self._intended: list[str] = []
        self._cut: tuple[str, int] | None = None

    # ── state ──────────────────────────────────────────────────────────
    @property
    def emitted_chars(self) -> int:
        """What has actually been written, in :func:`emit_cost`'s UTF-16 units.

        Named ``_chars`` for its callers' sake; the unit is code units, and for
        the all-BMP text every emitter carries today the two are identical.
        """
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
        self._mirror_hint = printable(hint)

    @property
    def room(self) -> int:
        """Characters still available to :meth:`emit`, after the reserve.

        Exposed so a caller sizing a truncation does not re-derive this
        arithmetic from the budget constant. Doing that by hand is how the
        reserve got counted twice: the caller subtracted its closing-line
        headroom again on top of the ceiling that already excludes it, and
        dropped content that would have fitted.
        """
        return max(0, (self._budget - self._reserve) - self._emitted)

    # ── writing ────────────────────────────────────────────────────────
    def fits(self, text: str, *, reserve: int = 0) -> bool:
        """Would ``text`` (plus a divider and ``reserve``) stay inside budget?

        ``reserve`` here is for output STILL TO COME that this writer does not
        know about (a pointer line for a later block). The closing-line reserve
        passed to ``__init__`` is already excluded from :attr:`room` — passing
        it again double-counts it.

        For GRACEFUL degrade — swap a large block for a pointer before emitting.
        Not a safety mechanism: :meth:`emit` enforces the budget regardless.

        Costed through :func:`emit_cost`, the SAME function :meth:`emit` bills
        with. They used to disagree by one character: `fits` charged the divider
        (8 = 7 chars + its newline) but not the content's own newline, so at the
        boundary it approved a block that `emit` then CUT. MEASURED: `fits()`
        approved 192 chars into 200 of room, and 169 of them were destroyed —
        the exact inversion of what this method exists to prevent, since its
        whole job is to swap a doomed block for a pointer BEFORE the cut.
        """
        return emit_cost(text) + _DIVIDER_COST + reserve <= self.room

    def emit_or_degrade(
        self,
        text: str,
        *,
        block: str,
        divider: str = "",
        pointer: str = "",
        notice: str = "",
        reserve: int = 0,
    ) -> str:
        """Emit ``text`` whole, or degrade it — the writer chooses, not the caller.

        THE POINT OF THIS METHOD is that the caller never computes characters.
        It says WHAT to degrade to; every number — the divider, the newline
        ``print`` adds, the closing-line reserve, what is already emitted — is
        settled here, once.

        That is not a tidiness preference. Callers doing this arithmetic by hand
        produced six defects in one review cycle of this module: the reserve
        counted twice, a `fits` that undercharged by one and approved blocks the
        writer then destroyed, a reserve of 120 for a 206-char line, a pointer
        reserved in a part that cannot emit it, and two constants documented
        with numbers measured from a test fixture rather than production. Every
        one of them lived in a caller re-deriving what this object already knows.

        :param pointer: a short stand-in emitted INSTEAD of ``text`` when the
            full text will not fit — the graceful degrade.
        :param notice: appended to a truncated prefix of ``text`` when even the
            pointer is not wanted. ``{kept}`` is substituted with the number of
            characters kept. Substituted, not ``format``-ed, so a brace in the
            surrounding prose cannot raise.
        :param reserve: room to hold back for output STILL TO COME that this
            writer does not know about. The constructor's closing-line reserve
            is already excluded from :attr:`room`; do not pass it again.
        :returns: ``"full"``, ``"pointer"``, ``"truncated"``, ``"notice-only"``
            (nothing of the text survived, but the notice naming the file and
            where to read it did) or ``"cut"`` — which branch was taken, so the
            caller can report it without re-deriving why.
        """
        # Made printable before anything is measured, so the degrade below
        # sizes the strings it will actually write (#3237).
        text, divider, pointer, notice = (printable(s) for s in (text, divider, pointer, notice))
        div_cost = emit_cost(divider) if divider else 0

        def _fits(s: str) -> bool:
            return emit_cost(s) + div_cost + reserve <= self.room

        def _write(s: str) -> None:
            if divider:
                self.emit(divider)
            self.emit(s, block=block)

        if _fits(text):
            _write(text)
            return "full"
        if pointer and _fits(pointer):
            _write(pointer)
            return "pointer"
        if notice:
            # The notice embeds the kept count, so its own length depends on the
            # answer. Budget against the WIDEST the number can be (room itself),
            # then render with the real, smaller value — so the emitted line is
            # never longer than what was budgeted for.
            widest = notice.replace("{kept}", str(self.room))
            # `emit_cost`, not `len`: the notice is CALLER-supplied text, so it
            # is the likeliest of these strings to carry an astral character.
            # Under-billing it would size `keep` too large and turn a graceful
            # degrade into the hard cut below.
            keep = max(0, self.room - reserve - div_cost - emit_cost(widest))
            rendered = notice.replace("{kept}", str(keep))
            # keep == 0 still emits the notice ALONE. The notice names the file
            # and where to read it, so it is worth more than the same number of
            # raw characters — and dropping to the raw text here would discard
            # the one line that tells the reader what is missing.
            _write(clip_to_cost(text, keep) + rendered)
            if self.closed:
                # The notice itself overran and tripped the cut path: the stream
                # is now closed and every later block will be dropped silently.
                # Report what HAPPENED, not the degrade that was attempted — a
                # caller told "notice-only" here would log a graceful degrade
                # over a hard cut. The notice is still emitted first because it
                # names the file and where to read it, which is worth more than
                # the same number of raw characters.
                return "cut"
            return "truncated" if keep else "notice-only"
        # No pointer, no notice, and it does not fit: fall through to `emit`,
        # which cuts LOUDLY and names the mirror — an existing, tested path.
        # Deliberately not a new silent branch: a degrade that cannot degrade is
        # exactly the condition that must stay audible.
        _write(text)
        return "cut"

    def emit(self, text: str, *, block: str = "") -> None:
        """Write ``text``, or as much of it as the budget allows.

        Always records ``text`` in :attr:`intended`, including after a cut, so
        the mirror stays complete no matter where the cut fell.

        ``text`` is made :func:`printable` first, and that form is what is
        billed, written and recorded, so the mirror matches the stream and a
        caller writing :attr:`intended` to disk cannot hit the encode error
        either.
        """
        text = printable(text)
        self._intended.append(text)
        if self.closed:
            return
        cost = emit_cost(text)
        ceiling = self._budget - self._reserve
        room = ceiling - self._emitted
        if cost <= room:
            self._write(text)
            return
        self._cut_here(text, block=block, room=room)

    def emit_final(self, text: str, fallback: str | None = None) -> None:
        """Write a closing line using the reserved headroom.

        Bypasses ``reserve`` (that is what it was reserved for) but never the
        budget, and is emitted even after a cut — the audit line reporting the
        cut is the one thing that must always land.

        ``fallback`` is a SHORTER form of the same line to emit WHOLE when the
        full text will not fit the remaining room. The writer chooses (the
        caller supplies candidates, never arithmetic): full if it fits, else a
        fitting fallback, else a raw clip as the genuine last resort. The clip
        cuts from the RIGHT, so a line whose tail is load-bearing — the audit
        line's ``— full text: <mirror>]_`` pointer — loses exactly the part the
        reader needs; the fallback exists so that outcome requires BOTH the full
        line AND its own shorter form to overflow, not merely the full line.
        """
        text = printable(text)
        if fallback is not None:
            fallback = printable(fallback)
        self._intended.append(text)
        room = self._budget - self._emitted
        if room <= 0:
            return
        if emit_cost(text) <= room:
            self._write(text)
        elif fallback is not None and emit_cost(fallback) <= room:
            self._write(fallback)
        else:
            self._write(clip_to_cost(text, max(0, room - 1)))

    # ── internals ──────────────────────────────────────────────────────
    def _write(self, text: str) -> None:
        # Every caller above already passes printable text, so this is the
        # identity on every path that exists. It stays because this is the one
        # place a strict encoder sees the string: a future path that forgets
        # would otherwise raise UnicodeEncodeError, which the OSError guard
        # below does not catch, and kill the hook (#3237).
        text = printable(text)
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
        self._emitted += emit_cost(text)

    def _cut_marker(self, block: str) -> str:
        where = f" — full text: {self._mirror_hint}" if self._mirror_hint else ""
        named = f" at '{block}'" if block else ""
        return (
            f"\n_[ctx {self._label}: CUT{named} — the harness files any hook output "
            f"over {HOOK_STDOUT_CAP} chars behind a ~2 KB preview, so the rest was "
            f"withheld here rather than risking the whole part{where}]_"
        )

    def _short_cut_marker(self) -> str:
        """The cut marker reduced to its load-bearing half: WHERE the full text is.

        Used when the full marker will not fit. The full one names the block and
        explains the cap before naming the mirror, so trimming it from the right
        removes exactly the part the reader needs — announcing a loss without
        saying where to recover it, which is worse than saying less.
        """
        return f"\n_[ctx {self._label}: CUT — full text: {self._mirror_hint or '(no mirror)'}]_"

    def _cut_here(self, text: str, *, block: str, room: int) -> None:
        """Emit what fits plus a loud marker, then close the stream."""
        marker = self._cut_marker(block)
        # Both writes cost their length PLUS the newline print() adds, so the
        # room for content is what is left after the marker and BOTH newlines.
        # Measured with `emit_cost` (which carries one of those newlines) rather
        # than `len`: this is the ONE path that calls `_write` directly instead
        # of going through `emit`, so nothing downstream re-checks the budget. A
        # marker billed in codepoints while the harness counts UTF-16 units
        # would leave `content_room` too large by the number of astral chars in
        # the mirror path or block label, and the pair would overrun the cap
        # while every check above reported success.
        content_room = room - emit_cost(marker) - _NEWLINE_COST
        kept = ""
        if content_room > 0:
            kept = clip_to_cost(text, content_room)
            self._write(kept)
            self._write(marker)
        else:
            # No room for both. The MARKER wins — a silent stop is the failure
            # this class exists to prevent — but it must not overshoot either.
            #
            # Trimming the FULL marker from the right drops the mirror path
            # first, because that path is at its end. That inverts the whole
            # point: the marker exists to turn data loss into a pointer, so the
            # pointer is the last thing that may go. Under pressure, emit a
            # short marker built mirror-first, and only trim that if even it
            # will not fit.
            room_now = (self._budget - self._reserve) - self._emitted
            short = self._short_cut_marker()
            if emit_cost(short) <= room_now:
                self._write(short)
            elif room_now > 1:
                self._write(clip_to_cost(short, room_now - 1))
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

    What a trimmed field keeps, in order of preference: the LONGEST prefix
    that fits followed by the full truncation note; failing that (the room
    beside the envelope is narrower than the note) the longest prefix that fits
    followed by a short note; failing that, nothing. Fields are trimmed largest
    serialised size first, each to the longest prefix that fits with the others
    as they stand. A payload that already fits is emitted unchanged.

    :param text_keys: dotted paths to trimmable strings, e.g.
        ``("hookSpecificOutput.additionalContext",)``.
    :returns: True if the emitted payload is WITHIN budget. False means it was
        emitted anyway and may be persisted — the caller's decision could be
        withheld from the model. False is returned exactly when the payload with
        every trimmable field emptied (the ENVELOPE) still exceeds the budget —
        including when no ``text_keys`` name a string, so nothing is trimmable;
        a warning naming the reason goes to stderr. Callers that need the
        guarantee must check this, because "something was trimmed" is not the
        same claim.
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
    # The fallback when even the full note does not fit beside the envelope:
    # a field that says it was cut, in fewer units, beats one that is silently
    # empty.
    short_note = "\n[truncated]"

    # What the STREAM receives, not what json.dumps returns: `print` appends a
    # newline and the harness counts it, so `emit_cost` bills it. Measuring only
    # the blob let a payload of exactly `budget` chars emit `budget + 1` while
    # reporting success.
    #
    # ONE unit throughout: the emitted cost of the whole serialised payload,
    # re-measured on every probe. The previous version mixed three — serialised
    # units for the overrun, Python characters for the field, UTF-16 units for
    # the clip — and since `json.dumps` writes "—" as the six-character
    # `—`, the overrun outgrew the field and the field was emptied (#3236).
    # Computing the cost of a prefix instead of measuring it would mean
    # restating JSON's escaping rules here; measuring cannot drift from them.
    def _cost() -> int:
        return emit_cost(json.dumps(payload))

    def _jlen(value: str) -> int:
        return len(json.dumps(value))

    def _fits() -> bool:
        return _cost() <= budget

    def _fit_prefix(key: str, base: str, marker: str) -> bool:
        """Set ``key`` to the LONGEST ``base`` prefix + ``marker`` that fits.

        Exact, not estimated: the payload's cost never falls as the prefix
        grows (``json.dumps`` escapes each character on its own, so a longer
        prefix serialises to a longer string), which makes a binary search over
        the prefix length return the longest one that fits in O(log n)
        measurements. The prefix stays strictly shorter than ``base``, because
        a marker on the whole text would announce a cut that did not happen.

        The probes measure the FIELD, not the payload: a JSON string is a
        self-delimited token, so the payload costs what it costs with the
        marker alone, plus the difference in the field's own serialised length.
        That keeps a probe proportional to the room (at most ``room``
        characters, since every character costs at least one unit) rather than
        to every other field in the payload. The result is then checked with
        the full measurement, and a failed check restores the field and
        returns False — an error in that reasoning can cost text, never
        produce a payload that claims to fit and does not.
        """
        before = _get(key) or ""
        _set(key, marker)
        room = budget - (_cost() - _jlen(marker))
        if not base or _jlen(marker) > room:
            _set(key, before)
            return False
        lo, hi = 0, min(len(base) - 1, room)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _jlen(base[:mid] + marker) <= room:
                lo = mid
            else:
                hi = mid - 1
        _set(key, base[:lo] + marker)
        if not _fits():
            _set(key, before)
            return False
        return True

    keys: list[str] = []
    for key in text_keys:
        if key not in keys and _get(key) is not None:
            keys.append(key)
    originals: dict[str, str] = {}
    for key in keys:
        value = _get(key) or ""
        # Never stack notes: text a caller already trimmed is trimmed afresh.
        originals[key] = value[: -len(note)] if value.endswith(note) else value

    if keys and not _fits():
        for key in keys:
            _set(key, "")
        envelope_fits = _fits()
        for key in keys:
            _set(key, originals[key])
        if not envelope_fits:
            # Nothing the text fields can give up will fit. Keep the old shape
            # for this case: each field the note would shorten carries the note.
            for key in keys:
                if _jlen(note) < _jlen(originals[key]):
                    _set(key, note)
        else:
            # The emptied envelope fits, so a fitting payload EXISTS and this
            # must find one (#3255: it used to stop at "note only", which can be
            # wider than the room, and report False). Longest SERIALISED field
            # first — the ranking used Python length, which under-ranks text
            # whose escapes cost six units a character. Each pass trims one
            # field to the longest prefix that fits; a field that cannot fit
            # even its marker is reduced to the marker and the next field pays.
            order = sorted(keys, key=lambda k: _jlen(originals[k]), reverse=True)
            done = False
            for marker in (note, short_note):
                for key in order:
                    if _fits() or _fit_prefix(key, originals[key], marker):
                        done = True
                        break
                    if _jlen(marker) < _jlen(_get(key) or ""):
                        _set(key, marker)
                if done:
                    break
            if not done:
                # Last resort, and the reason this branch always ends in
                # budget: an empty field. Reached only when the room left by
                # the envelope is smaller than even the short notes.
                for key in order:
                    if _fits():
                        break
                    _set(key, "")
                    print(
                        f"[hook_output] emptied {key!r} without a truncation note: "
                        "the envelope leaves no room for one.",
                        file=sys.stderr,
                    )

    # Callers outside tests, ENUMERATED rather than counted from memory
    # (`grep -rn print_json_bounded scripts/ src/`, no limit; 2026-10-19):
    # capped_read_advisory (two sites; checks the return value), genesis_stop_hook
    # (the Stop event's additionalContext), git_discard_guard (envelope backstop
    # behind its own whole-note selection), git_push_guard, issue_dup_advisory,
    # main_checkout_guard, plan_confidence_reminder (defence-in-depth — a fixed
    # string far under budget), pr_close_advisory and rm_trash_advisory (one
    # fixed-shape note each; the return value is not checked because a trimmed
    # note is still a true note and the hook enforces nothing), procedure_advisor,
    # and secrets_env_access_guard (a backstop behind the NOTE clips in
    # hook_ask_policy). This sentence has been wrong before in the same
    # direction — an adopting change falsified a status claim living in a file
    # the adopter never edits. If you add a caller, this line is part of the change.
    blob = json.dumps(payload)
    fits = emit_cost(blob) <= budget
    if not fits:
        # Emit ANYWAY: a decision the consumer never receives is worse than one
        # that may be previewed. But never claim success — an unreported
        # over-budget emit is exactly the silent loss this module exists for.
        print(
            f"[hook_output] payload is {emit_cost(blob)} chars against a {budget} "
            "budget and "
            "could not be trimmed further (no trimmable text_keys, or the envelope "
            "alone exceeds it) — the harness may persist it and withhold the decision.",
            file=sys.stderr,
        )
    print(blob, file=stream)
    stream.flush()
    return fits
