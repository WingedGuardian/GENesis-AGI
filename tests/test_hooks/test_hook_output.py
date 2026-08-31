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


def test_emit_final_lands_even_when_the_stream_was_cut(sink):
    """The audit line reporting the cut must never itself be the thing cut."""
    out = _writer(sink, budget=300, reserve=80)
    out.emit("x" * 900, block="knowledge")
    out.emit_final("_[ctx knowledge: 9000 -> 300 chars]_")
    assert "_[ctx knowledge:" in sink.getvalue()


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
