"""The bounded-stdout chokepoint: output CANNOT cross the harness cap.

Claude Code files any hook stdout over 10,000 chars behind a ~2 KB preview, and
says nothing. The emitter this module was extracted from checked its budget in
2 of 12 blocks — the other ten were emit-and-hope, which is how ~30 KB of
identity/charter went missing from 195 windows. These tests pin the property
that makes forgetting impossible: the WRITER enforces, not the caller.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("hook_output", _HOOKS_DIR / "hook_output.py")
_ho = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ho)


@pytest.fixture
def sink() -> io.StringIO:
    return io.StringIO()


def _writer(sink: io.StringIO, budget: int = 200, **kw) -> _ho.BoundedStdout:
    kw.setdefault("label", "knowledge")
    return _ho.BoundedStdout(budget, stream=sink, **kw)


def test_cap_constant_is_the_measured_harness_threshold():
    """The measured value lives in exactly one place.

    10,000 was MEASURED on CC 2.1.246 (10,000 inline / 10,001 filed) and READ
    from the bundle as the literal on the hook-persistence path. A second copy
    of this number elsewhere is the drift that already broke a test once.
    """
    assert _ho.HOOK_STDOUT_CAP == 10_000
    assert _ho.DEFAULT_BUDGET < _ho.HOOK_STDOUT_CAP


@pytest.mark.parametrize("text", ["timeout", "😀timeout", "😀" * 20])
@pytest.mark.parametrize("remaining", [0, 1, 2, 3, 4, 5, 20, 10000])
def test_final_notice_preserves_locked_count_and_newline_margin(text, remaining):
    emitted = _ho.HOOK_STDOUT_CAP - remaining
    notice = _ho.clip_final_notice(text, emitted_units=emitted)
    if remaining <= 3:
        assert notice is None
    else:
        assert notice is not None
        # Both framing newlines plus the spare unit fit, including astral text.
        assert emitted + _ho.utf16_len("\n" + notice + "\n") < _ho.HOOK_STDOUT_CAP
        assert text.startswith(notice)
        if remaining >= _ho.utf16_len(text) + 3:
            assert notice == text


def test_under_budget_emits_everything_uncut(sink):
    out = _writer(sink)
    out.emit("alpha", block="a")
    out.emit("beta", block="b")
    assert sink.getvalue() == "alpha\nbeta\n"
    assert out.cut is None
    assert not out.closed


def test_oversized_emit_is_cut_at_the_budget_not_beyond(sink):
    out = _writer(sink, budget=200)
    out.emit("x" * 500, block="essential-knowledge")
    assert len(sink.getvalue()) <= 200, len(sink.getvalue())
    assert out.emitted_chars <= 200


def test_cut_marker_names_the_block_and_the_mirror(sink):
    out = _writer(sink, budget=300, mirror_hint="~/.genesis/sessions/s1/context-knowledge.md")
    out.emit("x" * 900, block="essential-knowledge")
    rendered = sink.getvalue()
    assert "CUT" in rendered
    assert "essential-knowledge" in rendered
    assert "context-knowledge.md" in rendered, rendered
    assert out.cut is not None
    block, dropped = out.cut
    assert block == "essential-knowledge"
    assert dropped > 0


def test_writes_after_a_cut_are_dropped_from_the_stream(sink):
    out = _writer(sink, budget=200)
    out.emit("x" * 500, block="first")
    before = sink.getvalue()
    out.emit("SHOULD-NOT-APPEAR", block="second")
    assert sink.getvalue() == before
    assert "SHOULD-NOT-APPEAR" not in sink.getvalue()


def test_intended_keeps_the_whole_text_even_after_a_cut(sink):
    """The mirror is what makes a cut recoverable rather than data loss."""
    out = _writer(sink, budget=200)
    out.emit("x" * 500, block="first")
    out.emit("SHOULD-NOT-APPEAR", block="second")
    assert "x" * 500 in out.intended
    assert "SHOULD-NOT-APPEAR" in out.intended
    assert out.intended_chars > out.emitted_chars


def test_emit_final_lands_WHOLE_not_merely_present(sink):
    """The audit line reporting the cut must never itself be the thing cut.

    Asserted as `endswith`, not as a substring of its own prefix. The previous
    version checked `"_[ctx knowledge:" in output` — 16 characters — which a
    line truncated to 17 still satisfies. It was therefore structurally unable
    to catch the defect it names, and it did not: in production the reserve was
    120 while the cut-variant line is ~206, so the line lost its trailing `]_`
    on every real cut and this test stayed green.
    """
    line = (
        "_[ctx knowledge: 9000 intended / 300 emitted — CUT 8700 chars at 'ek' — full text: /x/y]_"
    )
    out = _writer(sink, budget=300, reserve=_ho.emit_cost(line))
    out.emit("x" * 900, block="knowledge")
    out.emit_final(line)
    assert sink.getvalue().endswith(line + "\n"), sink.getvalue()[-90:]


def test_emit_final_prefers_a_pointer_fallback_over_clipping_the_tail(sink):
    """When the full closing line will not fit, a caller-supplied fallback that
    fits is emitted WHOLE — the raw clip (which drops the line's tail, and the
    tail is the mirror pointer) is the last resort, not the first.

    Reproduces F1: `emit_final` used to `clip_to_cost` from the right, so an
    audit line whose counters or session id pushed it past the room lost its
    trailing `— full text: <mirror>]_` — the one part the reader needs. The
    fallback drops the volatile counters and keeps part + pointer.
    """
    full = (
        "_[ctx knowledge: 123456789012 intended / 9800 emitted — CUT 123456779212 chars at 'ek' — full text: /home/u/.genesis/sessions/"
        + "s" * 60
        + "/context-knowledge.md]_"
    )
    fallback = (
        "_[ctx knowledge: audit counts omitted for size — full text: /home/u/.genesis/sessions/"
        + "s" * 60
        + "/context-knowledge.md]_"
    )
    # Room fits the fallback but not the full line.
    reserve = _ho.emit_cost(fallback)
    out = _writer(sink, budget=500, reserve=reserve)
    out.emit("x" * (500 - reserve), block="knowledge")
    out.emit_final(full, fallback=fallback)
    got = sink.getvalue()
    assert got.rstrip("\n").endswith("context-knowledge.md]_"), got[-90:]
    assert "audit counts omitted" in got, "fell back to a raw clip instead of the pointer form"


def test_reserve_is_held_back_from_emit(sink):
    """A block may not spend the headroom kept for the closing line."""
    out = _writer(sink, budget=200, reserve=100)
    out.emit("y" * 150, block="b")
    assert out.cut is not None  # 150 > 200-100, so it must cut
    assert out.emitted_chars <= 200


def test_fits_respects_the_reserve(sink):
    out = _writer(sink, budget=200, reserve=100)
    assert out.fits("z" * 50)
    assert not out.fits("z" * 150)


# ── JSON: the envelope is never sacrificed ─────────────────────────────


def test_json_under_budget_is_emitted_unchanged(sink):
    payload = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "hi"}}
    fits = _ho.print_json_bounded(
        payload, text_keys=("hookSpecificOutput.additionalContext",), stream=sink
    )
    assert fits
    assert json.loads(sink.getvalue()) == payload


def test_json_over_budget_trims_text_but_keeps_the_decision(sink):
    """A dropped decision is a fail-open; a shortened reason is a nuisance."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "R" * 400,
        }
    }
    fits = _ho.print_json_bounded(
        payload,
        text_keys=("hookSpecificOutput.permissionDecisionReason",),
        budget=200,
        stream=sink,
    )
    assert fits
    out = json.loads(sink.getvalue())
    assert len(sink.getvalue()) <= 200, len(sink.getvalue())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "truncated" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_json_returns_false_when_it_cannot_fit(sink, capsys):
    """The return value must mean "it fits", not "I touched something".

    A caller asking for the guarantee this module advertises — that a decision
    is never withheld for size — cannot get it from a "did I trim?" boolean.
    Reported on stderr too, because an unreported over-budget emit is exactly
    the silent loss the module exists to prevent.
    """
    payload = {"hookSpecificOutput": {"permissionDecision": "deny", "big": "B" * 400}}
    fits = _ho.print_json_bounded(payload, text_keys=(), budget=100, stream=sink)
    assert fits is False
    # emitted ANYWAY — a decision the consumer never receives is worse
    assert json.loads(sink.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "could not be trimmed further" in capsys.readouterr().err


def test_json_returns_false_when_the_envelope_alone_is_oversize(sink, capsys):
    payload = {"hookSpecificOutput": {"permissionDecision": "deny"}, "a": "x" * 50}
    fits = _ho.print_json_bounded(payload, text_keys=("a",), budget=20, stream=sink)
    assert fits is False
    assert capsys.readouterr().err


def test_json_returns_false_for_an_absent_or_nonstring_key(sink, capsys):
    payload = {"decision": "deny", "n": 12345, "big": "B" * 300}
    fits = _ho.print_json_bounded(payload, text_keys=("n", "missing.path"), budget=100, stream=sink)
    assert fits is False
    assert json.loads(sink.getvalue())["decision"] == "deny"
    assert capsys.readouterr().err


def test_json_trims_the_longest_named_field_first(sink):
    payload = {"a": "x" * 300, "b": "y" * 20, "keep": "decision"}
    assert _ho.print_json_bounded(payload, text_keys=("a", "b"), budget=150, stream=sink)
    out = json.loads(sink.getvalue())
    assert out["keep"] == "decision"
    assert out["b"] == "y" * 20  # short field untouched
    assert "truncated" in out["a"]


def test_json_never_stacks_truncation_notes(sink):
    payload = {"a": "x" * 5_000, "keep": "decision"}
    _ho.print_json_bounded(payload, text_keys=("a",), budget=200, stream=sink)
    assert json.loads(sink.getvalue())["a"].count("truncated") == 1


# ── review round 2: what actually reaches the stream ───────────────────


@pytest.mark.parametrize("slack", [-2, -1, 0, 1, 2])
def test_a_true_return_always_means_the_STREAM_fits(slack):
    """The guarantee is about the STREAM, not about ``json.dumps``.

    ``print(blob, file=stream)`` appends a newline and the harness counts it,
    so a payload serialising to exactly ``budget`` characters EMITTED
    ``budget + 1`` while this function returned True. One character, in the
    direction that says "fine" as the decision is withheld — the failure this
    module exists to prevent, inside the module itself.

    Asserted as the invariant rather than at one hand-picked size: returning
    False and emitting anyway is a documented, honest outcome, so the property
    is not "it always fits" but "it never CLAIMS to fit when it did not". A
    sweep across the boundary catches the off-by-one wherever it sits.
    """
    sink = io.StringIO()
    payload = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ""}}
    overhead = len(json.dumps(payload))
    pad = 300
    payload["hookSpecificOutput"]["additionalContext"] = "p" * pad
    budget = overhead + pad + slack

    fits = _ho.print_json_bounded(
        payload, text_keys=("hookSpecificOutput.additionalContext",), budget=budget, stream=sink
    )
    if fits:
        assert len(sink.getvalue()) <= budget, (
            f"claimed to fit a {budget}-char budget while emitting {len(sink.getvalue())}"
        )
    if slack >= 1:
        # Without this the whole assertion above sits under `if fits`, and a
        # regression returning False unconditionally passes every cell — a test
        # that can be satisfied by refusing to do the thing it is testing.
        # A budget at or above the payload has room to spare, so False there is
        # a real failure, not an honest refusal.
        assert fits, f"a {budget}-char budget fits this payload; refusing it is a regression"


def test_json_reports_the_emitted_size_not_the_blob_size(sink, capsys):
    """The stderr warning is a number an operator reconciles against the cap."""
    payload = {"decision": "deny", "reason": "r" * 400}
    _ho.print_json_bounded(payload, text_keys=(), budget=50, stream=sink)
    warning = capsys.readouterr().err
    assert str(len(sink.getvalue())) in warning


def test_room_excludes_what_is_emitted_and_the_reserve(sink):
    out = _writer(sink, budget=200, reserve=100)
    assert out.room == 100
    out.emit("z" * 49, block="b")  # 49 chars + the newline print adds
    assert out.room == 50


def test_fits_does_not_double_count_the_constructor_reserve(sink):
    """``reserve`` on :meth:`fits` is for output STILL TO COME.

    The closing-line reserve is already excluded from ``room``; a caller that
    passed it again reserved it twice and discarded content that would have
    fitted. Measured against ``room`` so the assertion cannot drift with the
    divider constant.
    """
    out = _writer(sink, budget=200, reserve=100)
    # Sized from the CONSTRUCTOR values, not from `room`. Deriving the text
    # from the property under test makes the assertion self-consistent under
    # any definition of it — the test then passes whatever `room` returns,
    # which is how a test ends up pinning nothing at all.
    text = "z" * (200 - 100 - _ho._DIVIDER_COST - _ho._NEWLINE_COST)
    assert out.fits(text), "text sized to the available room must fit"
    assert not out.fits(text, reserve=1), "a pending extra character must not"


def test_a_block_fits_approves_is_never_then_cut(sink):
    """`fits` and `emit` must cost a write the SAME way.

    They disagreed by one character: `fits` charged the divider (7 chars plus
    its newline) but not the content's own newline, so at the boundary it
    approved a block the enforcing write then destroyed. MEASURED before the
    fix: 192 chars approved into 200 of room, 169 of them cut. That inverts the
    method's entire purpose — it exists to swap a doomed block for a pointer
    BEFORE the cut, and instead it waved the block through.

    Swept across the boundary rather than asserted at one width, because an
    off-by-one is invisible everywhere except at the edge.
    """
    for slack in range(-3, 4):
        sink = io.StringIO()
        out = _writer(sink, budget=200)
        text = "z" * (out.room - _ho._DIVIDER_COST - _ho._NEWLINE_COST + slack)
        approved = out.fits(text)
        out.emit("\n\n---\n\n")  # the divider `fits` was budgeting for
        out.emit(text, block="body")
        if approved:
            assert out.cut is None, (
                f"fits() approved {len(text)} chars at slack={slack}, "
                f"then emit cut {out.cut[1] if out.cut else 0} of them"
            )


def test_a_degrade_that_actually_cut_reports_cut_not_notice_only(sink):
    """The return value must describe what HAPPENED, not what was attempted.

    When `keep` reaches zero the notice is still worth emitting — it names the
    file and where to read it. But if the notice itself overruns, `emit` trips
    the cut path: the stream closes and every later block is dropped silently.
    Returning "notice-only" there tells the caller a graceful degrade occurred
    over a hard cut, and it is the caller that reports the part's fate.
    """
    out = _writer(sink, budget=50, mirror_hint="~/.genesis/sessions/s1/context-identity-core.md")
    verdict = out.emit_or_degrade(
        "S" * 4000,
        block="identity:SOUL.md",
        notice="\n\n_[SOUL.md truncated at {kept} chars — read the file for the rest]_",
    )
    assert out.closed, "premise: this degrade must actually have tripped the cut path"
    assert verdict == "cut", f"reported {verdict!r} for a hard cut"


def test_a_marker_too_big_to_fit_still_names_the_mirror(sink):
    """Under pressure the POINTER is the last thing that may be dropped.

    The full marker names the block and explains the cap before naming the
    mirror, and it was trimmed from the RIGHT — so the mirror path went first,
    leaving a marker that announces a loss without saying where to recover it.
    That inverts the marker's purpose: it exists to turn data loss into a
    pointer.
    """
    out = _writer(sink, budget=75, mirror_hint="~/.g/s1/context-knowledge.md")
    out.emit("x" * 4000, block="a-block-with-a-deliberately-long-name")
    rendered = sink.getvalue()
    assert "context-knowledge.md" in rendered, rendered
    assert len(rendered) <= 75, "the marker must not overshoot the budget it is trimmed for"


def _units(s: str) -> int:
    """What the harness counts: UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


@pytest.mark.parametrize(
    "label,text",
    [
        ("astral only", "\U0001f3af" * 20_000),
        ("astral + bmp", "a\U0001f3af" * 10_000),
        ("bmp control", "A" * 20_000),
    ],
)
def test_the_budget_holds_in_the_units_the_harness_counts(sink, label, text):
    """The writer's ONE guarantee, measured in the harness's own unit.

    `emit_cost` bills UTF-16 code units; every slice used to cut by CODEPOINTS.
    For astral text (emoji, rarer CJK) that is a silent 2x overrun — the writer
    keeps N characters believing they cost N, and they cost 2N.

    MEASURED before the fix: 19,384 units emitted against a 10,000 cap while the
    writer reported itself within budget. The harness then files the whole part,
    which is the exact silent context loss this module exists to prevent, and the
    cut marker naming the mirror is destroyed along with it — so the reader loses
    both the content AND the pointer to it.

    Found by a cross-model reviewer AFTER the unit fix landed: correcting the
    accounting without correcting the slicing left the two disagreeing, which is
    worse than having them consistently wrong. The BMP row is the control — it
    must be unaffected, or the fix is just a smaller budget.
    """
    out = _writer(sink, budget=9_800, mirror_hint="~/.genesis/sessions/s1/context-knowledge.md")
    out.emit(text, block="essential-knowledge")
    rendered = sink.getvalue()

    assert _units(rendered) <= 9_800, f"{label}: {_units(rendered)} units over a 9,800 budget"
    assert "context-knowledge.md" in rendered, f"{label}: the cut marker lost its mirror pointer"


def test_emit_final_also_bills_and_slices_in_the_same_unit(sink):
    """The closing line is the COMPLETION PROOF — it must not overshoot either.

    Same class as the cut path: `emit_final` sliced codepoints against a unit
    budget, so an astral audit line could push the part over the cap on the one
    write whose whole job is to report what happened.
    """
    out = _writer(sink, budget=300, reserve=200, mirror_hint="~/.genesis/sessions/s1/x.md")
    out.emit("B" * 400, block="b")
    out.emit_final("\U0001f3af" * 500)
    assert _units(sink.getvalue()) <= 300


def test_clip_to_cost_never_splits_a_surrogate_pair(sink):
    """An odd unit budget must not cut an astral character in half.

    Counting whole characters makes this structural: a 2-unit character is
    either kept whole or dropped. Slicing the encoded bytes would produce a lone
    surrogate, which is unencodable and would crash the write.
    """
    clipped = _ho.clip_to_cost("\U0001f3af" * 10, 5)  # odd budget, 2-unit chars
    assert _units(clipped) <= 5
    clipped.encode("utf-8")  # a lone surrogate would raise here
    assert clipped == "\U0001f3af" * 2


def test_an_astral_mirror_path_does_not_push_the_cut_pair_over_the_cap(sink):
    """The MARKER's own size is a budget input, so it must be billed in units too.

    `_cut_here` is the one path that calls `_write` directly instead of going
    through `emit`, so nothing downstream re-checks its arithmetic: it sizes the
    content against the marker itself. Measuring the marker with `len` while the
    harness counts UTF-16 units leaves room for exactly as many extra units as
    the marker has astral characters, and the content+marker pair then overruns
    the cap while every check reports success.

    The astral characters here are in the MIRROR PATH — a real possibility, since
    the path embeds a session id and a part name that Genesis does not control
    character-by-character. Overrun before the fix is one unit per astral char.
    """
    hint = "~/.genesis/sessions/" + "\U0001f3af" * 30 + "/context-knowledge.md"
    out = _writer(sink, budget=9_800, mirror_hint=hint)
    out.emit("A" * 20_000, block="essential-knowledge")

    rendered = sink.getvalue()
    assert _units(rendered) <= 9_800, (
        f"{_units(rendered)} units over a 9,800 budget — the marker was billed in codepoints"
    )
    assert hint in rendered, "the cut marker lost its mirror pointer"


def test_an_astral_notice_still_degrades_gracefully_instead_of_cutting(sink):
    """A caller-supplied notice is the likeliest of these strings to carry astral text.

    `emit_or_degrade` sizes `keep` against the widest rendering of the notice.
    Billing that with `len` makes `keep` too large by the notice's astral count,
    so the write it then composes overruns and trips the HARD CUT path — turning
    the graceful degrade this function exists to provide into the loss it exists
    to avoid. The verdict is the assertion: "truncated" is the degrade,
    "cut" means the budget arithmetic was wrong.
    """
    out = _writer(sink, budget=400, mirror_hint="~/.genesis/sessions/s1/x.md")
    verdict = out.emit_or_degrade(
        "B" * 2_000,
        block="conversation",
        notice="\n_[kept {kept} of it \U0001f3af\U0001f3af\U0001f3af\U0001f3af\U0001f3af]_",
    )

    assert verdict == "truncated", f"graceful degrade became {verdict!r}"
    assert _units(sink.getvalue()) <= 400


# ── #3236: trimming non-ASCII text keeps the longest prefix that fits ──


_NOTE = f"\n[truncated — over the {_ho.HOOK_STDOUT_CAP}-char hook output cap]"
_SHORT_NOTE = "\n[truncated]"


def _nested(text: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "additionalContext": text,
        }
    }


def _trim_one(text: str, budget: int = _ho.DEFAULT_BUDGET) -> tuple[bool, str, io.StringIO]:
    sink = io.StringIO()
    ok = _ho.print_json_bounded(
        _nested(text),
        text_keys=("hookSpecificOutput.additionalContext",),
        budget=budget,
        stream=sink,
    )
    return ok, json.loads(sink.getvalue())["hookSpecificOutput"]["additionalContext"], sink


def test_3236_repro_em_dashes_are_trimmed_not_emptied():
    """The issue's exact repro: 2,000 em dashes kept NONE of the text.

    `json.dumps` writes "—" as the six-character `\\u2014`, so the overrun in
    serialised units outgrew the field's Python length and `keep` fell to 0.
    Room for 1,605 dashes plus the note existed, per the issue's own arithmetic.
    """
    ok, ctx, sink = _trim_one("—" * 2_000)

    assert ok
    assert ctx.endswith(_NOTE)
    kept = ctx[: -len(_NOTE)]
    assert kept == "—" * len(kept)
    assert len(kept) >= 1_605, len(kept)
    assert _units(sink.getvalue()) <= _ho.DEFAULT_BUDGET


@pytest.mark.parametrize(
    ("length", "every"),
    [(12_000, 10), (12_000, 5), (20_000, 20), (20_000, 10), (50_000, 100), (50_000, 20)],
)
def test_3236_sweep_keeps_the_maximal_prefix(length, every):
    """The issue's sweep rows that kept 0 or too little text, at 1-20% non-ASCII.

    Asserted as MAXIMALITY rather than a number: one more character of the
    original pushes the stream over budget. That is the contract `clip_to_cost`
    already meets, now met by the JSON writer too.
    """
    text = "".join("—" if i % every == every - 1 else "a" for i in range(length))
    ok, ctx, sink = _trim_one(text)

    assert ok
    assert _units(sink.getvalue()) <= _ho.DEFAULT_BUDGET
    assert ctx.endswith(_NOTE)
    kept = ctx[: -len(_NOTE)]
    assert kept and text.startswith(kept)
    longer = json.dumps(_nested(text[: len(kept) + 1] + _NOTE))
    assert _ho.emit_cost(longer) > _ho.DEFAULT_BUDGET


def test_3236_fields_are_ranked_by_serialised_size_not_python_length(sink):
    """300 em dashes serialise to 1,802 units; 1,000 ASCII letters to 1,002.

    Ranked by Python length the ASCII field was trimmed first, though the
    em-dash field is the larger one on the wire.
    """
    payload = {"a": "—" * 300, "b": "y" * 1_000, "keep": "decision"}
    assert _ho.print_json_bounded(payload, text_keys=("a", "b"), budget=2_500, stream=sink)
    out = json.loads(sink.getvalue())
    assert out["b"] == "y" * 1_000
    assert out["a"].endswith(_NOTE)


# ── #3255: True whenever the emptied envelope fits ─────────────────────


def test_3255_repro_emptied_envelope_fitting_exactly_returns_true(sink, capsys):
    """The issue's minimised case: the emptied envelope costs 45 against a 45 budget.

    The old loop stopped at "note only", which is wider than the room, and
    reported False although `{"permissionDecision": "deny", "reason": ""}` fits.
    """
    payload = {"permissionDecision": "deny", "reason": "x" * 80}
    envelope = _ho.emit_cost(json.dumps({"permissionDecision": "deny", "reason": ""}))
    assert envelope == 45

    assert _ho.print_json_bounded(payload, text_keys=("reason",), budget=45, stream=sink)
    assert json.loads(sink.getvalue()) == {"permissionDecision": "deny", "reason": ""}
    assert _units(sink.getvalue()) <= 45
    # An empty field carries no marker of its own, so the cut is reported.
    assert "without a truncation note" in capsys.readouterr().err


def test_3255_short_note_is_used_when_only_it_fits(sink):
    """Between "full note fits" and "nothing fits": a field that says it was cut."""
    payload = {"permissionDecision": "deny", "reason": "x" * 80}
    budget = _ho.emit_cost(json.dumps({"permissionDecision": "deny", "reason": _SHORT_NOTE}))

    assert _ho.print_json_bounded(payload, text_keys=("reason",), budget=budget, stream=sink)
    assert json.loads(sink.getvalue())["reason"] == _SHORT_NOTE


def test_3255_a_second_field_pays_when_the_first_cannot_fit_its_note(sink):
    """Room for the envelope, one full note and 30 more units, across two fields."""
    payload = {"permissionDecision": "deny", "a": "x" * 400, "b": "y" * 100}
    room = _ho.emit_cost(json.dumps({"permissionDecision": "deny", "a": _NOTE, "b": "y" * 30}))

    assert _ho.print_json_bounded(payload, text_keys=("a", "b"), budget=room, stream=sink)
    out = json.loads(sink.getvalue())
    assert out["permissionDecision"] == "deny"
    for key, filler in (("a", "x"), ("b", "y")):
        assert out[key].endswith((_NOTE, _SHORT_NOTE)), out[key]
        assert set(out[key].split("\n")[0]) <= {filler}
    assert _units(sink.getvalue()) <= room


# ── #3237: a lone surrogate is escaped, billed and written identically ─


def test_3237_repro_measurement_does_not_raise():
    assert _ho.utf16_len("\ud800") == 6
    assert _ho.emit_cost("\ud800") == 7
    assert _ho.printable("ok \ud800") == "ok \\ud800"


def _utf8_strict_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(
        buf, encoding="utf-8", errors="strict", newline="\n", write_through=True
    )


def test_3237_repro_emit_writes_what_it_billed():
    """`print("ok \\ud800")` to a strict UTF-8 stream raises; the writer must not.

    And the escape it writes instead is what it billed: the issue's design note
    is that changing only the count leaves the write crashing, and changing only
    the write leaves the count wrong.
    """
    buf, stream = _utf8_strict_stream()
    out = _ho.BoundedStdout(label="x", stream=stream)
    out.emit("ok \ud800")
    stream.flush()

    assert buf.getvalue() == b"ok \\ud800\n"
    assert out.emitted_chars == _units(buf.getvalue().decode("utf-8"))
    # The mirror records the written form, so writing it to disk cannot raise.
    assert out.intended == "ok \\ud800"
    out.intended.encode("utf-8")


def test_3237_repro_print_bounded_does_not_raise(capsys):
    out = _ho.print_bounded("ok \ud800", label="x")
    assert capsys.readouterr().out == "ok \\ud800\n"
    assert out.cut is None


def test_3237_every_writer_input_is_made_printable():
    """Label, mirror, divider, pointer, notice and the final line all reach the stream."""
    buf, stream = _utf8_strict_stream()
    out = _ho.BoundedStdout(90, label="l\udc80", mirror_hint="m\udc81", stream=stream)
    out.emit_or_degrade(
        "T\ud800" * 50,
        block="b",
        divider="\ud801",
        pointer="p\ud806" * 40,
        notice="[kept {kept}\ud802]",
    )
    out.emit("after a cut \ud803" * 20)
    out.emit_final("done \ud804", fallback="d\ud805")
    stream.flush()

    text = buf.getvalue().decode("utf-8")
    assert "\\ud802" in text  # the notice landed, escaped
    assert _units(text) <= 90
    assert out.emitted_chars == _units(text)


def test_3237_a_cut_marker_with_a_surrogate_mirror_stays_in_budget():
    """A path decoded with surrogateescape can carry a lone low surrogate."""
    buf, stream = _utf8_strict_stream()
    out = _ho.BoundedStdout(400, label="knowledge", mirror_hint="/x/\udcff.md", stream=stream)
    out.emit("A" * 900, block="body")
    stream.flush()

    text = buf.getvalue().decode("utf-8")
    assert out.cut is not None
    assert "\\udcff" in text
    assert _units(text) <= 400
    assert out.emitted_chars == _units(text)


def test_printable_is_the_identity_for_text_without_surrogates():
    """ASCII and every valid non-ASCII string pass through as the SAME object.

    That is what keeps every writer byte-identical to before for such text: the
    only change on those paths is a `printable()` call that returns its input.
    """
    for text in ("", "plain ascii", "é — 漢 🎯", "\t\n\x00"):
        assert _ho.printable(text) is text
        assert _ho.utf16_len(text) == len(text.encode("utf-16-le")) // 2
