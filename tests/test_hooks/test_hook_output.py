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
