"""Property coverage for the hook-output budget chokepoint."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from tests.conftest import private_module

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "hooks"
_ho = private_module("hook_output_properties_under_test", _HOOKS_DIR / "hook_output.py")

settings.register_profile(
    "genesis_hook_output_properties",
    derandomize=True,
    max_examples=75,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.load_profile("genesis_hook_output_properties")

_TRUNCATION_NOTE = f"\n[truncated — over the {_ho.HOOK_STDOUT_CAP}-char hook output cap]"

_ASCII = st.characters(min_codepoint=0x20, max_codepoint=0x7E)
_UNICODE_EDGE = st.sampled_from(["é", "Ω", "漢", "🎯", "🧪", "\t", "\n"])
_TEXT = st.text(alphabet=st.one_of(_ASCII, _UNICODE_EDGE), max_size=120)
_NON_ASCII_BMP = st.characters(
    min_codepoint=0x80,
    max_codepoint=0xD7FF,
    blacklist_categories=("Cs",),
)
_LONE_SURROGATE_TEXT = st.lists(
    st.sampled_from(["\ud800", "\ud801", "\udc00", "\udfff"]),
    min_size=1,
    max_size=8,
).map("".join)


def _stream_cost(stream: io.StringIO) -> int:
    return _ho.utf16_len(stream.getvalue())


@given(text=_TEXT, budget=st.integers(min_value=-10, max_value=300))
@example(text="🎯", budget=1)
def test_clip_to_cost_returns_the_longest_prefix_that_fits(text: str, budget: int):
    clipped = _ho.clip_to_cost(text, budget)

    assert text.startswith(clipped)
    assert _ho.utf16_len(clipped) <= max(0, budget)
    clipped.encode("utf-8")

    if len(clipped) < len(text) and budget > 0:
        next_char = text[len(clipped)]
        assert _ho.utf16_len(clipped + next_char) > budget


@given(
    reason=_TEXT,
    context=_TEXT,
    budget=st.integers(min_value=120, max_value=900),
)
def test_print_json_bounded_true_means_the_emitted_stream_fits(
    reason: str,
    context: str,
    budget: int,
):
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": context,
        },
        "decision": "block",
    }
    original = copy.deepcopy(payload)
    sink = io.StringIO()

    fits = _ho.print_json_bounded(
        payload,
        text_keys=(
            "hookSpecificOutput.permissionDecisionReason",
            "hookSpecificOutput.additionalContext",
        ),
        budget=budget,
        stream=sink,
    )
    emitted = json.loads(sink.getvalue())

    assert emitted["decision"] == original["decision"]
    assert (
        emitted["hookSpecificOutput"]["hookEventName"]
        == original["hookSpecificOutput"]["hookEventName"]
    )
    assert (
        emitted["hookSpecificOutput"]["permissionDecision"]
        == original["hookSpecificOutput"]["permissionDecision"]
    )
    if fits:
        assert _stream_cost(sink) <= budget


@given(
    budget=st.integers(min_value=1, max_value=220),
    mode=st.sampled_from(("no_keys", "missing_key", "nonstring_key", "envelope_too_large")),
)
def test_print_json_bounded_false_is_confined_to_documented_cases(
    budget: int,
    mode: str,
):
    required = "R" * (budget + 50)
    payload = {"permissionDecision": "deny", "required": required, "reason": "r" * 20}
    if mode == "no_keys":
        text_keys = ()
    elif mode == "missing_key":
        text_keys = ("missing.path",)
    elif mode == "nonstring_key":
        payload["reason"] = 123
        text_keys = ("reason",)
    else:
        envelope = copy.deepcopy(payload)
        envelope["reason"] = ""
        assume(_ho.emit_cost(json.dumps(envelope)) > budget)
        text_keys = ("reason",)

    sink = io.StringIO()
    fits = _ho.print_json_bounded(payload, text_keys=text_keys, budget=budget, stream=sink)

    assert json.loads(sink.getvalue())["permissionDecision"] == "deny"
    assert fits is False


@given(
    chunks=st.lists(_TEXT, max_size=12),
    budget=st.integers(min_value=1, max_value=700),
    reserve=st.integers(min_value=0, max_value=120),
    label=_TEXT,
    mirror_hint=_TEXT,
)
@example(chunks=["🎯"], budget=2, reserve=0, label="hook", mirror_hint="")
def test_bounded_stdout_never_emits_over_budget(
    chunks: list[str],
    budget: int,
    reserve: int,
    label: str,
    mirror_hint: str,
):
    sink = io.StringIO()
    out = _ho.BoundedStdout(
        budget,
        label=label or "hook",
        reserve=reserve,
        mirror_hint=mirror_hint,
        stream=sink,
    )

    for index, chunk in enumerate(chunks):
        out.emit(chunk, block=f"block-{index}")
    out.emit_final("done", fallback="d")

    assert _stream_cost(sink) <= budget
    assert out.emitted_chars <= budget


@given(text=_TEXT, budget=st.integers(min_value=1, max_value=700), mirror_hint=_TEXT)
@example(text="🎯", budget=2, mirror_hint="")
def test_print_bounded_never_emits_over_budget(text: str, budget: int, mirror_hint: str):
    sink = io.StringIO()

    with contextlib.redirect_stdout(sink):
        out = _ho.print_bounded(text, label="hook", budget=budget, mirror_hint=mirror_hint)

    assert _stream_cost(sink) <= budget
    assert out.emitted_chars <= budget


@pytest.mark.xfail(
    strict=True,
    reason="#3236: non-ASCII trimming leaves avoidable budget unused",
)
@given(size=st.integers(min_value=80, max_value=130), over=st.integers(min_value=1, max_value=25))
def test_xfail_3236_non_ascii_trimming_uses_the_available_budget(size: int, over: int):
    base = "é" * size
    payload = {"reason": base}
    budget = _ho.emit_cost(json.dumps(payload)) - over
    assume(budget > _ho.emit_cost(json.dumps({"reason": _TRUNCATION_NOTE})))

    sink = io.StringIO()
    assert _ho.print_json_bounded(payload, text_keys=("reason",), budget=budget, stream=sink)

    trimmed = json.loads(sink.getvalue())["reason"]
    assert trimmed.endswith(_TRUNCATION_NOTE)
    kept = trimmed[: -len(_TRUNCATION_NOTE)]
    assume(len(kept) < len(base))

    candidate = {"reason": kept + base[len(kept)] + _TRUNCATION_NOTE}
    assert _ho.emit_cost(json.dumps(candidate)) > budget


@pytest.mark.xfail(
    strict=True,
    reason="#3237: lone surrogate strings can raise UnicodeEncodeError",
)
@given(text=_LONE_SURROGATE_TEXT, budget=st.integers(min_value=1, max_value=80))
def test_xfail_3237_lone_surrogates_do_not_raise(text: str, budget: int):
    _ho.emit_cost(text)
    _ho.clip_to_cost(text, budget)
    sink = io.StringIO()
    out = _ho.BoundedStdout(budget, label="hook", stream=sink)
    out.emit(text, block="surrogate")
    assert _stream_cost(sink) <= budget
