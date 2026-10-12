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

_PROPERTY_SETTINGS = settings(
    derandomize=True,
    max_examples=75,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

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


def _get_path(payload: dict[str, object], path: str) -> object:
    node: object = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(payload: dict[str, object], path: str, value: str) -> None:
    node: object = payload
    parts = path.split(".")
    for part in parts[:-1]:
        child = node[part] if isinstance(node, dict) else None
        if not isinstance(child, dict):
            return
        node = child
    if isinstance(node, dict):
        node[parts[-1]] = value


def _emptied_envelope(
    payload: dict[str, object],
    text_keys: tuple[str, ...],
) -> dict[str, object]:
    envelope = copy.deepcopy(payload)
    for path in text_keys:
        if isinstance(_get_path(envelope, path), str):
            _set_path(envelope, path, "")
    return envelope


def _permission_decision(payload: dict[str, object]) -> object:
    return _get_path(payload, "permissionDecision") or _get_path(
        payload, "hookSpecificOutput.permissionDecision"
    )


@st.composite
def _json_bounded_case(draw) -> tuple[dict[str, object], tuple[str, ...], int]:
    mode = draw(
        st.sampled_from(
            (
                "ordinary_trimmable",
                "ordinary_nested_trimmable",
                "no_keys",
                "missing_key",
                "nonstring_key",
                "envelope_too_large",
            )
        )
    )

    if mode == "ordinary_trimmable":
        reason = draw(st.text(alphabet=st.one_of(_ASCII, _UNICODE_EDGE), max_size=180))
        payload = {"permissionDecision": "deny", "reason": reason}
        text_keys = ("reason",)
        noted = {"permissionDecision": "deny", "reason": _TRUNCATION_NOTE}
        floor = _ho.emit_cost(json.dumps(noted))
        ceiling = max(floor, _ho.emit_cost(json.dumps(payload)) + 20)
        return payload, text_keys, draw(st.integers(min_value=floor, max_value=ceiling))

    if mode == "ordinary_nested_trimmable":
        context = draw(st.text(alphabet=st.one_of(_ASCII, _UNICODE_EDGE), max_size=180))
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "additionalContext": context,
            }
        }
        text_keys = ("hookSpecificOutput.additionalContext",)
        noted = copy.deepcopy(payload)
        noted["hookSpecificOutput"]["additionalContext"] = _TRUNCATION_NOTE
        floor = _ho.emit_cost(json.dumps(noted))
        ceiling = max(floor, _ho.emit_cost(json.dumps(payload)) + 20)
        return payload, text_keys, draw(st.integers(min_value=floor, max_value=ceiling))

    if mode == "no_keys":
        payload = {"permissionDecision": "deny", "required": "R" * 260}
        text_keys = ()
        envelope = payload
    elif mode == "missing_key":
        payload = {"permissionDecision": "deny", "required": "R" * 260}
        text_keys = ("missing.path",)
        envelope = payload
    elif mode == "nonstring_key":
        payload = {"permissionDecision": "deny", "reason": 123, "required": "R" * 260}
        text_keys = ("reason",)
        envelope = payload
    else:
        payload = {"permissionDecision": "deny", "required": "R" * 260, "reason": "r" * 20}
        text_keys = ("reason",)
        envelope = _emptied_envelope(payload, text_keys)

    envelope_cost = _ho.emit_cost(json.dumps(envelope))
    return payload, text_keys, draw(st.integers(min_value=1, max_value=envelope_cost - 1))


@_PROPERTY_SETTINGS
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


@_PROPERTY_SETTINGS
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


@_PROPERTY_SETTINGS
@given(case=_json_bounded_case())
def test_print_json_bounded_false_is_confined_to_documented_cases(
    case: tuple[dict[str, object], tuple[str, ...], int],
):
    payload, text_keys, budget = case
    original = copy.deepcopy(payload)
    envelope = _emptied_envelope(original, text_keys)
    sink = io.StringIO()
    fits = _ho.print_json_bounded(payload, text_keys=text_keys, budget=budget, stream=sink)
    emitted = json.loads(sink.getvalue())

    assert _permission_decision(emitted) == "deny"
    if fits:
        assert _stream_cost(sink) <= budget
    else:
        assert _ho.emit_cost(json.dumps(envelope)) > budget


@_PROPERTY_SETTINGS
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


@_PROPERTY_SETTINGS
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
@_PROPERTY_SETTINGS
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
    reason="#3237: lone surrogate accounting can raise UnicodeEncodeError",
)
@_PROPERTY_SETTINGS
@given(text=_LONE_SURROGATE_TEXT, budget=st.integers(min_value=1, max_value=80))
def test_xfail_3237_lone_surrogate_accounting_does_not_raise(text: str, budget: int):
    _ho.emit_cost(text)
    _ho.clip_to_cost(text, budget)


@pytest.mark.xfail(
    strict=True,
    reason="#3237: lone surrogate writes can raise UnicodeEncodeError",
)
@_PROPERTY_SETTINGS
@given(text=_LONE_SURROGATE_TEXT, budget=st.integers(min_value=1, max_value=80))
def test_xfail_3237_lone_surrogate_writer_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    budget: int,
):
    def surrogatepass_emit_cost(value: str) -> int:
        return len(value.encode("utf-16-le", "surrogatepass")) // 2 + 1

    monkeypatch.setattr(_ho, "emit_cost", surrogatepass_emit_cost)
    encoded_stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    out = _ho.BoundedStdout(budget, label="hook", stream=encoded_stream)
    out.emit(text, block="surrogate")
    encoded_stream.flush()
