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


def _clip_char_cost(text: str) -> int:
    """clip_to_cost's own unit: 2 for astral, 1 otherwise (including lone surrogates)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


_NONEMPTY_TEXT = st.text(alphabet=st.one_of(_ASCII, _UNICODE_EDGE), min_size=1, max_size=180)
_ESCAPE_CLASS_FILLERS = ("é", "—", "漢", "🎯")

_ORDINARY_BOUNDARY_PAYLOAD = {"permissionDecision": "deny", "reason": "x" * 80}
_ORDINARY_BOUNDARY_KEYS = ("reason",)
_ORDINARY_BOUNDARY_ENVELOPE_COST = _ho.emit_cost(
    json.dumps(_emptied_envelope(_ORDINARY_BOUNDARY_PAYLOAD, _ORDINARY_BOUNDARY_KEYS))
)

_TWO_KEY_BOUNDARY_KEYS = (
    "hookSpecificOutput.permissionDecisionReason",
    "hookSpecificOutput.additionalContext",
)
_TWO_KEY_BOUNDARY_PAYLOAD = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "R" * 40,
        "additionalContext": "C" * 40,
    },
    "decision": "block",
}
_TWO_KEY_BOUNDARY_ENVELOPE_COST = _ho.emit_cost(
    json.dumps(_emptied_envelope(_TWO_KEY_BOUNDARY_PAYLOAD, _TWO_KEY_BOUNDARY_KEYS))
)


@st.composite
def _ordinary_payload(draw) -> tuple[dict[str, object], tuple[str, ...], int, int]:
    """Flat, nested, and two-key trimmable payloads with emptied/original costs."""
    shape = draw(st.sampled_from(("one_flat", "one_nested", "two_key")))
    if shape == "one_flat":
        payload: dict[str, object] = {
            "permissionDecision": "deny",
            "reason": draw(_NONEMPTY_TEXT),
        }
        text_keys: tuple[str, ...] = ("reason",)
    elif shape == "one_nested":
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "additionalContext": draw(_NONEMPTY_TEXT),
            }
        }
        text_keys = ("hookSpecificOutput.additionalContext",)
    else:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": draw(_NONEMPTY_TEXT),
                "additionalContext": draw(_NONEMPTY_TEXT),
            },
            "decision": "block",
        }
        text_keys = _TWO_KEY_BOUNDARY_KEYS
    envelope_cost = _ho.emit_cost(json.dumps(_emptied_envelope(payload, text_keys)))
    original_cost = _ho.emit_cost(json.dumps(payload))
    return payload, text_keys, envelope_cost, original_cost


@st.composite
def _ordinary_boundary_case(draw) -> tuple[dict[str, object], tuple[str, ...], int]:
    """Ordinary trimmable payloads budgeted from emptied-envelope cost through original."""
    payload, text_keys, envelope_cost, original_cost = draw(_ordinary_payload())
    budget = draw(
        st.integers(min_value=envelope_cost, max_value=max(envelope_cost, original_cost))
    )
    return payload, text_keys, budget


@st.composite
def _below_envelope_case(draw) -> tuple[dict[str, object], tuple[str, ...], int]:
    """Documented False path: budget is one unit below the emptied envelope."""
    payload, text_keys, envelope_cost, _original_cost = draw(_ordinary_payload())
    return payload, text_keys, envelope_cost - 1


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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#3255: print_json_bounded can return False when the emptied envelope fits",
)
@_PROPERTY_SETTINGS
@given(case=_ordinary_boundary_case())
@example(
    case=(
        copy.deepcopy(_ORDINARY_BOUNDARY_PAYLOAD),
        _ORDINARY_BOUNDARY_KEYS,
        _ORDINARY_BOUNDARY_ENVELOPE_COST,
    )
)
@example(
    case=(
        copy.deepcopy(_ORDINARY_BOUNDARY_PAYLOAD),
        _ORDINARY_BOUNDARY_KEYS,
        _ORDINARY_BOUNDARY_ENVELOPE_COST + 1,
    )
)
@example(
    case=(
        copy.deepcopy(_TWO_KEY_BOUNDARY_PAYLOAD),
        _TWO_KEY_BOUNDARY_KEYS,
        _TWO_KEY_BOUNDARY_ENVELOPE_COST,
    )
)
@example(
    case=(
        copy.deepcopy(_TWO_KEY_BOUNDARY_PAYLOAD),
        _TWO_KEY_BOUNDARY_KEYS,
        _TWO_KEY_BOUNDARY_ENVELOPE_COST + 1,
    )
)
def test_xfail_print_json_bounded_false_when_emptied_envelope_fits(
    case: tuple[dict[str, object], tuple[str, ...], int],
):
    payload, text_keys, budget = case
    original = copy.deepcopy(payload)
    envelope = _emptied_envelope(original, text_keys)
    sink = io.StringIO()
    fits = _ho.print_json_bounded(payload, text_keys=text_keys, budget=budget, stream=sink)
    emitted = json.loads(sink.getvalue())

    assert _permission_decision(emitted) == "deny"
    if len(text_keys) > 1:
        for key in text_keys:
            value = _get_path(original, key)
            assert isinstance(value, str) and value
    if fits:
        assert _stream_cost(sink) <= budget
    else:
        assert _ho.emit_cost(json.dumps(envelope)) > budget


@_PROPERTY_SETTINGS
@given(case=_below_envelope_case())
@example(
    case=(
        copy.deepcopy(_ORDINARY_BOUNDARY_PAYLOAD),
        _ORDINARY_BOUNDARY_KEYS,
        _ORDINARY_BOUNDARY_ENVELOPE_COST - 1,
    )
)
@example(
    case=(
        copy.deepcopy(_TWO_KEY_BOUNDARY_PAYLOAD),
        _TWO_KEY_BOUNDARY_KEYS,
        _TWO_KEY_BOUNDARY_ENVELOPE_COST - 1,
    )
)
def test_print_json_bounded_false_when_emptied_envelope_exceeds_budget(
    case: tuple[dict[str, object], tuple[str, ...], int],
):
    payload, text_keys, budget = case
    original = copy.deepcopy(payload)
    envelope = _emptied_envelope(original, text_keys)
    sink = io.StringIO()
    fits = _ho.print_json_bounded(payload, text_keys=text_keys, budget=budget, stream=sink)
    emitted = json.loads(sink.getvalue())

    assert _permission_decision(emitted) == "deny"
    assert fits is False
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
@given(text=_TEXT, budget=st.integers(min_value=1, max_value=700))
@example(text="🎯", budget=2)
@example(text="🎯", budget=1)
def test_encoded_stream_writer_uses_production_accounting(text: str, budget: int):
    buf = io.BytesIO()
    stream = io.TextIOWrapper(
        buf, encoding="utf-8", errors="strict", newline="\n", write_through=True
    )
    out = _ho.BoundedStdout(budget, label="hook", stream=stream)
    out.emit(text, block="body")
    stream.flush()
    raw = buf.getvalue()
    decoded = raw.decode("utf-8")
    encoded = decoded.encode("utf-8")
    utf16_le = decoded.encode("utf-16-le")

    assert encoded == raw
    billed = _ho.utf16_len(decoded)
    assert billed == len(utf16_le) // 2
    assert billed == out.emitted_chars
    assert billed <= budget
    assert out.emitted_chars <= budget
    for char in decoded:
        if ord(char) > 0xFFFF:
            assert char.encode("utf-8") in raw


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
    raises=AssertionError,
    reason="#3236: non-ASCII trimming leaves avoidable budget unused",
)
@_PROPERTY_SETTINGS
@given(
    filler=st.sampled_from(_ESCAPE_CLASS_FILLERS),
    size=st.integers(min_value=80, max_value=130),
    over=st.integers(min_value=1, max_value=25),
    nested=st.booleans(),
)
@example(filler="é", size=80, over=1, nested=False)
@example(filler="🎯", size=80, over=1, nested=True)
@example(filler="—", size=80, over=1, nested=True)
def test_xfail_3236_non_ascii_trimming_uses_the_available_budget(
    filler: str,
    size: int,
    over: int,
    nested: bool,
):
    base = filler * size
    if nested:
        payload: dict[str, object] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "additionalContext": base,
            }
        }
        text_keys = ("hookSpecificOutput.additionalContext",)
        field_path = "hookSpecificOutput.additionalContext"
    else:
        payload = {"reason": base}
        text_keys = ("reason",)
        field_path = "reason"
    original = copy.deepcopy(payload)
    budget = _ho.emit_cost(json.dumps(original)) - over
    note_only = copy.deepcopy(original)
    _set_path(note_only, field_path, _TRUNCATION_NOTE)
    assume(budget > _ho.emit_cost(json.dumps(note_only)))

    sink = io.StringIO()
    assert _ho.print_json_bounded(payload, text_keys=text_keys, budget=budget, stream=sink)

    trimmed = _get_path(json.loads(sink.getvalue()), field_path)
    assert isinstance(trimmed, str)
    assert trimmed.endswith(_TRUNCATION_NOTE)
    kept = trimmed[: -len(_TRUNCATION_NOTE)]
    assume(len(kept) < len(base))

    candidate = copy.deepcopy(original)
    _set_path(candidate, field_path, kept + base[len(kept)] + _TRUNCATION_NOTE)
    assert _ho.emit_cost(json.dumps(candidate)) > budget


@_PROPERTY_SETTINGS
@given(text=_LONE_SURROGATE_TEXT, budget=st.integers(min_value=1, max_value=80))
def test_clip_to_cost_handles_lone_surrogates(text: str, budget: int):
    clipped = _ho.clip_to_cost(text, budget)

    assert text.startswith(clipped)
    assert _clip_char_cost(clipped) <= max(0, budget)
    clipped.encode("utf-8", "surrogatepass")

    if len(clipped) < len(text) and budget > 0:
        next_char = text[len(clipped)]
        assert _clip_char_cost(clipped + next_char) > budget


@pytest.mark.xfail(
    strict=True,
    raises=UnicodeEncodeError,
    reason="#3237: lone surrogate accounting can raise UnicodeEncodeError",
)
@_PROPERTY_SETTINGS
@given(text=_LONE_SURROGATE_TEXT)
def test_xfail_3237_lone_surrogate_accounting_does_not_raise(text: str):
    _ho.emit_cost(text)


@pytest.mark.xfail(
    strict=True,
    raises=UnicodeEncodeError,
    reason="#3237: lone surrogate writes can raise UnicodeEncodeError",
)
@_PROPERTY_SETTINGS
@given(text=_LONE_SURROGATE_TEXT, budget=st.integers(min_value=1, max_value=80))
@example(text="\ud800", budget=2)
def test_xfail_3237_lone_surrogate_writer_does_not_raise(text: str, budget: int):
    buf = io.BytesIO()
    encoded_stream = io.TextIOWrapper(
        buf, encoding="utf-8", errors="strict", newline="\n", write_through=True
    )
    out = _ho.BoundedStdout(budget, label="hook", stream=encoded_stream)
    out.emit(text, block="surrogate")
    encoded_stream.flush()
    raw = buf.getvalue()
    decoded = raw.decode("utf-8", "surrogatepass")
    billed = _ho.utf16_len(decoded)
    assert billed == out.emitted_chars
    assert billed <= budget
    if out.cut is None and billed > 0:
        encodings = (
            text.encode("utf-8", "surrogatepass"),
            text.encode("utf-8", "replace"),
            text.encode("unicode_escape"),
        )
        assert any(form in raw for form in encodings)
    else:
        assert _ho.emit_cost(text) > budget
