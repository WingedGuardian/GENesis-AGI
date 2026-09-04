"""A "missing argument" error must not hide a malformed call.

When a long free-text argument is emitted with a closing tag that does not match
its opening tag, every parameter declared AFTER it is absorbed into that string.
The server then reports those parameters as *missing*, which reads like the
caller forgot them or like a tool bug. Measured on this install: six consecutive
identical refusals of one `follow_up_create` call before the shape was
diagnosed, with the evidence sitting in the error's own `input_value` throughout.

Two layers are exercised: the pure diagnosis, and the middleware wiring that
actually delivers it to a client through the real FastMCP pipeline. The second
matters because a diagnosis nothing raises is a diagnosis nobody reads.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError, validate_call

from genesis.observability.mcp_arg_diagnostics import (
    absorbed_parameter_hint,
    missing_argument_names,
)
from genesis.observability.mcp_middleware import InstrumentationMiddleware
from genesis.observability.provider_activity import ProviderActivityTracker


def _missing_error(**provided) -> ValidationError:
    """A REAL pydantic missing_argument error, not a hand-built stand-in.

    Constructing the exception by hand would let the module pass while pydantic's
    actual error shape drifted underneath it — the failure mode this whole file
    exists to catch one layer up.
    """

    @validate_call
    def demo(content: str, reason: str, work_state: str) -> str:
        return content + reason + work_state

    with pytest.raises(ValidationError) as excinfo:
        demo(**provided)
    return excinfo.value


# ── the diagnosis ───────────────────────────────────────────────────────


def test_missing_names_come_from_the_structured_errors():
    exc = _missing_error(content="x")

    assert missing_argument_names(exc) == ["reason", "work_state"]


def test_an_absorbed_parameter_is_named_as_the_real_cause():
    """The shape that actually occurred: the markup for a 'missing' parameter is
    sitting inside a provided one."""
    absorbed = 'the real content</content>\n<parameter name="reason">because</parameter>'
    exc = _missing_error(content=absorbed)

    hint = absorbed_parameter_hint(exc, {"content": absorbed})

    assert hint is not None
    assert "NOT a missing value" in hint
    assert "`content`" in hint
    assert "reason" in hint
    assert "re-sending the same call" in hint


def test_the_truncation_fingerprint_is_reported_when_present():
    absorbed = 'body<parameter name="work_state">ready</parameter>\n</domain>'
    exc = _missing_error(content=absorbed)

    hint = absorbed_parameter_hint(exc, {"content": absorbed})

    assert "</domain>" in hint


@pytest.mark.parametrize(
    "value",
    [
        # Prose ABOUT the markup, naming no missing parameter. This repository's
        # own documentation contains exactly this, so a bare closing tag must
        # never be sufficient evidence on its own.
        "Check that every closing tag matches: a mismatched </content> swallows "
        "the rest of the call.",
        "no markup at all, just ordinary prose",
        "",
    ],
)
def test_prose_that_names_no_missing_parameter_is_left_alone(value):
    """TRUE-NEGATIVE CONTROL. Without these, a function returning the hint
    unconditionally would satisfy every test above."""
    exc = _missing_error(content=value)

    assert absorbed_parameter_hint(exc, {"content": value}) is None


def test_a_non_validation_error_is_left_alone():
    assert absorbed_parameter_hint(RuntimeError("boom"), {"content": "x"}) is None


def test_a_validation_error_with_no_missing_arguments_is_left_alone():
    """A wrong TYPE is a real caller mistake; its message must survive."""

    @validate_call
    def demo(count: int) -> int:
        return count

    with pytest.raises(ValidationError) as excinfo:
        demo(count="not a number")

    assert absorbed_parameter_hint(excinfo.value, {"count": "not a number"}) is None


def test_non_string_arguments_do_not_crash_the_scan():
    exc = _missing_error(content="x")

    assert absorbed_parameter_hint(exc, {"content": 5, "other": None, "l": [1]}) is None


# ── the wiring, through the real FastMCP pipeline ───────────────────────


def _server_with_middleware() -> FastMCP:
    mcp = FastMCP("diag-probe")

    @mcp.tool
    def demo(content: str, reason: str, work_state: str) -> str:
        """Shaped like follow_up_create: free text first, required params after."""
        return f"{content}|{reason}|{work_state}"

    mcp.add_middleware(InstrumentationMiddleware(ProviderActivityTracker(), "diag", db=None))
    return mcp


def _call(arguments: dict):
    async def go():
        async with Client(_server_with_middleware()) as client:
            return await client.call_tool("demo", arguments)

    return asyncio.run(go())


def test_the_client_receives_the_diagnosis_not_the_missing_argument_error():
    """END TO END. The pure function is useless if the middleware does not raise
    it, and only the real pipeline proves a substituted error reaches a client."""
    absorbed = 'text</content>\n<parameter name="reason">why</parameter>'

    with pytest.raises(Exception) as excinfo:
        _call({"content": absorbed})

    message = str(excinfo.value)
    assert "NOT a missing value" in message
    assert "absorbed" in message


def test_an_ordinary_missing_argument_still_reports_normally():
    """TRUE-NEGATIVE CONTROL at the wiring layer: a caller who genuinely omitted
    a parameter must still be told that, not handed a malformed-call theory."""
    with pytest.raises(Exception) as excinfo:
        _call({"content": "a perfectly ordinary sentence"})

    message = str(excinfo.value)
    assert "NOT a missing value" not in message
    assert "reason" in message


def test_a_well_formed_call_returns_its_normal_result():
    """The safety property at the return value. "Behaves exactly as before" also
    covers the transaction boundary, which is checked separately below — this
    one pins only the result, and says so rather than implying more."""
    result = _call({"content": "a", "reason": "b", "work_state": "ready"})

    assert result.content[0].text == "a|b|ready"


def test_a_type_error_is_not_rewritten_as_a_malformed_call():
    """The `missing_argument` type filter must be load-bearing.

    Found by mutation: deleting the type check survived the whole suite. The
    case that kills it needs an error of a DIFFERENT type whose `loc` names a
    parameter that also appears as markup elsewhere — then, without the filter,
    a genuine type error is rewritten as a malformed-call story and the real
    problem (a string where an int belongs) is hidden from the caller.

    That is the failure mode this module exists to prevent, pointed the other
    way, so it gets its own pin.
    """

    @validate_call
    def demo(content: str, count: int) -> str:
        return content * count

    absorbed = 'text<parameter name="count">5</parameter>'
    with pytest.raises(ValidationError) as excinfo:
        demo(content=absorbed, count="not a number")

    assert missing_argument_names(excinfo.value) == []
    assert (
        absorbed_parameter_hint(excinfo.value, {"content": absorbed, "count": "not a number"})
        is None
    )


# ── the pins that would have caught the two defects an audit found ──────


def test_evidence_at_the_tail_of_a_very_long_argument_is_still_found():
    """REGRESSION PIN. The scan bound was a HEAD window; absorption appends the
    swallowed markup to the TAIL.

    So the longer the free-text argument, the more certainly the evidence fell
    outside the window — the module went blind on exactly the population it
    exists for, and silently, since the caller just got the original misleading
    error back. MEASURED on the broken version: fired at 199,000 characters,
    SILENT at 200,000. The module's own docstring and the hint's closing advice
    both talk about LONG arguments, so it contradicted itself.
    """
    marker = '</content>\n<parameter name="reason">because</parameter>'
    for prefix_len in (199_000, 200_000, 800_000):
        value = ("x" * prefix_len) + marker
        hint = absorbed_parameter_hint(_missing_error(content=value), {"content": value})
        assert hint is not None, f"went silent at prefix length {prefix_len:,}"
        assert "reason" in hint


def test_a_parameter_with_no_evidence_is_not_claimed_to_have_been_swallowed():
    """REGRESSION PIN. The message asserted that EVERY reported-missing
    parameter was "sent and swallowed" while only one was evidenced.

    A caller who mismatched a tag AND genuinely omitted another parameter was
    told to re-send "with each parameter in its own block", and would fail again
    on the one they never had. That is the confident-wrong-explanation this
    module's own docstring forbids, committed inside the module that forbids it.
    """
    partial = 'do the thing</contents>\n<parameter name="reason">because</parameter>'

    hint = absorbed_parameter_hint(_missing_error(content=partial), {"content": partial})

    assert hint is not None
    # `reason` is evidenced; `work_state` is not and must be flagged as possibly
    # genuinely absent rather than asserted swallowed.
    assert "reason was sent and swallowed" in hint
    assert "No markup was found for work_state" in hint
    assert "may genuinely be absent" in hint


def test_every_missing_parameter_evidenced_leaves_no_caveat():
    """The other direction: when all of them ARE evidenced, no hedge is added."""
    absorbed = (
        'body</content>\n<parameter name="reason">r</parameter>\n'
        '<parameter name="work_state">ready</parameter>'
    )

    hint = absorbed_parameter_hint(_missing_error(content=absorbed), {"content": absorbed})

    assert "reason, work_state were sent and swallowed" in hint
    assert "may genuinely be absent" not in hint


# ── the invariants the substitution must not disturb ────────────────────


class _FakeDB:
    """Records the per-call transaction boundary the middleware owns."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


def _call_with_db(arguments: dict, db):
    mcp = FastMCP("diag-db")

    @mcp.tool
    def demo(content: str, reason: str, work_state: str) -> str:
        return f"{content}|{reason}|{work_state}"

    mcp.add_middleware(InstrumentationMiddleware(ProviderActivityTracker(), "diag", db=db))

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("demo", arguments)

    return asyncio.run(go())


def test_the_substituted_path_still_rolls_the_transaction_back():
    """The middleware also owns a commit-on-success / rollback-on-error boundary
    whose whole purpose is releasing a read snapshot that would otherwise pin
    the WAL checkpoint. Substituting the error must not skip it — `success` has
    to be set to False BEFORE the replacement is raised."""
    db = _FakeDB()
    absorbed = 'text</content>\n<parameter name="reason">why</parameter>'

    with pytest.raises(ToolError):
        _call_with_db({"content": absorbed}, db)

    assert db.calls == ["rollback"]


def test_a_well_formed_call_still_commits():
    """The safety property, checked at the boundary that matters rather than
    only on the return value: a complete call commits exactly as before."""
    db = _FakeDB()

    result = _call_with_db({"content": "a", "reason": "b", "work_state": "ready"}, db)

    assert result.content[0].text == "a|b|ready"
    assert db.calls == ["commit"]


def test_the_original_error_is_kept_as_the_cause():
    """The replacement must CHAIN, not erase. The message is substituted for the
    caller; the underlying ValidationError has to survive for anything reading
    __cause__ (tracebacks, error reporting)."""
    absorbed = 'text</content>\n<parameter name="reason">why</parameter>'
    hint = absorbed_parameter_hint(_missing_error(content=absorbed), {"content": absorbed})
    assert hint is not None  # precondition: this input does trigger the substitution

    err = ToolError(hint)
    try:
        raise err from _missing_error(content=absorbed)
    except ToolError as raised:
        assert isinstance(raised.__cause__, ValidationError)


def test_the_feature_depends_on_a_fastmcp_default_that_is_pinned_here():
    """Reachability rests on a third-party default, so it is asserted, not assumed.

    With `strict_input_validation` True, the MCP lowlevel server runs a
    jsonschema check BEFORE any middleware, argument binding never reaches this
    middleware, and this whole feature is dead code with no signal. If this
    assertion ever fails, the diagnosis is silently gone — that is the finding,
    not a broken test.
    """
    from fastmcp import settings

    assert settings.strict_input_validation is False


# ── the three defects a second cross-model round found ──────────────────


def test_a_marker_in_the_middle_of_a_huge_value_is_still_found():
    """REGRESSION PIN. Splicing head+tail dropped the middle entirely.

    The splice was itself the fix for an earlier finding (a HEAD-only window
    went blind on long values, because absorption appends to the TAIL). It
    replaced one blind spot with two: a marker in the middle of a value longer
    than twice the window vanished, and a marker could be FABRICATED across the
    join from fragments adjacent in neither half. Both are gone because there is
    no window any more — `finditer` scans the whole value without copying it, at
    roughly 0.8 ms/MB on a call that has already failed.
    """
    marker = '<parameter name="reason">because</parameter>'
    value = ("a" * 200_050) + marker + ("b" * 200_050)

    hint = absorbed_parameter_hint(_missing_error(content=value), {"content": value})

    assert hint is not None, "a marker in the middle of a large value was dropped"
    assert "reason" in hint


def test_a_spliced_window_can_no_longer_fabricate_a_marker():
    """The other half of the same defect, asserted structurally.

    Concatenating a head slice and a tail slice can create a substring spanning
    the join that exists in NEITHER — including a well-formed parameter marker
    naming a genuinely-absent argument. Scanning the value itself makes the class
    unreachable: what is searched is exactly what was sent.
    """
    half_open = '<parameter name="rea'
    half_close = 'son">y</parameter>'
    value = ("x" * 199_980) + half_open + ("M" * 400) + half_close + ("z" * 199_980)

    assert '<parameter name="reason">' not in value, "the probe itself is malformed"

    hint = absorbed_parameter_hint(_missing_error(content=value), {"content": value})

    assert hint is None, "a marker was fabricated from fragments never adjacent"


def test_a_mixed_validation_error_is_left_intact():
    """REGRESSION PIN. Replacing the whole exception discarded the other errors.

    Pydantic can report a missing_argument AND an independent type error in one
    ValidationError. Substituting a hint that mentions only the tag mismatch
    silently dropped the second problem — so the caller could not fix everything
    on the "first retry" this feature exists to enable, which defeats its whole
    purpose. The replacement is now offered only when EVERY entry is a diagnosed
    missing argument.
    """

    @validate_call
    def mixed(content: str, count: int, reason: str) -> str:
        return content

    absorbed = 'body<parameter name="reason">r</parameter>'
    with pytest.raises(ValidationError) as excinfo:
        mixed(content=absorbed, count="not-an-int")

    hint = absorbed_parameter_hint(excinfo.value, {"content": absorbed, "count": "not-an-int"})

    assert hint is None, "a mixed error was replaced, hiding the type error"


def test_a_marker_without_a_closing_tag_is_not_enough_evidence():
    """A marker ALONE is weak: prose describing the syntax contains one.

    A real absorbed emission also carries the closing tag, because the parameter
    that got swallowed was itself well-formed. Requiring both is strictly
    stronger evidence and costs no true positive — every genuine case in this
    file still fires.
    """
    prose = 'the docs mention <parameter name="reason"> as the opening form'

    assert absorbed_parameter_hint(_missing_error(content=prose), {"content": prose}) is None
