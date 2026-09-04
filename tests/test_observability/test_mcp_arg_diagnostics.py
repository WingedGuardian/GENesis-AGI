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
    assert "POSSIBLY a malformed tool call" in hint
    assert "`content`" in hint
    assert "reason" in hint
    assert "re-sending the call unchanged will fail identically" in hint
    # BOTH readings must be offered. That is what makes a false positive
    # harmless, and it is the property that replaced three rounds of predicate
    # tightening.
    assert "If the markup is deliberate prose" in hint


def test_the_truncation_fingerprint_is_reported_when_present():
    """The fixture carries the shape a real absorption has: the holder's own
    closing tag arrives BEFORE the markup it failed to close. An earlier version
    put the marker first, which no real emission does — invented data that the
    structural check correctly rejects."""
    absorbed = 'body</contents>\n<parameter name="work_state">ready</parameter>\n</domain>'
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
    assert "POSSIBLY a malformed tool call" in message
    assert "absorbed" in message


def test_an_ordinary_missing_argument_still_reports_normally():
    """TRUE-NEGATIVE CONTROL at the wiring layer: a caller who genuinely omitted
    a parameter must still be told that, not handed a malformed-call theory."""
    with pytest.raises(Exception) as excinfo:
        _call({"content": "a perfectly ordinary sentence"})

    message = str(excinfo.value)
    assert "POSSIBLY a malformed tool call" not in message
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
    # `reason` is evidenced; `work_state` is not, and must be flagged as more
    # likely genuinely absent rather than asserted swallowed.
    assert "markup naming reason" in hint
    assert "No such markup was found for work_state" in hint
    assert "more likely genuinely absent" in hint


def test_every_missing_parameter_evidenced_leaves_no_caveat():
    """The other direction: when all of them ARE evidenced, no hedge is added."""
    absorbed = (
        'body</content>\n<parameter name="reason">r</parameter>\n'
        '<parameter name="work_state">ready</parameter>'
    )

    hint = absorbed_parameter_hint(_missing_error(content=absorbed), {"content": absorbed})

    assert "markup naming reason, work_state" in hint
    assert "more likely genuinely absent" not in hint


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

    # Exercised at the MIDDLEWARE, which is where the chain has to exist.
    #
    # Two earlier versions of this test were wrong in opposite directions. The
    # first constructed and raised its own exception, so deleting `from exc` in
    # the middleware left it green — it tested Python's `raise ... from`. The
    # second asserted through the CLIENT, where `__cause__` is always None:
    # MEASURED, the chain does not survive the MCP protocol boundary, because the
    # error is serialized and reconstructed. That is a property of the transport,
    # not a defect, and nothing should claim client-visible chaining.
    #
    # What matters is the server-side chain, for tracebacks and error reporting.
    original = _missing_error(content=absorbed)

    class _Msg:
        # Matched to the error's own title, because the binding check added for
        # the body-raised case compares them. A real binding failure is titled
        # `call[<tool>]`; this helper's error is titled after the local function,
        # so the tool name is taken from the error rather than hard-coded — the
        # alternative would be a fixture that silently skips the substitution and
        # a test that passes for the wrong reason.
        name = getattr(original, "title", "demo")
        arguments = {"content": absorbed}

    class _Ctx:
        message = _Msg()

    async def _raise(_ctx):
        raise original

    mw = InstrumentationMiddleware(ProviderActivityTracker(), "diag", db=None)

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(mw.on_call_tool(_Ctx(), _raise))

    assert excinfo.value.__cause__ is original


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
    # Shaped like a real absorption: the holder's own closing tag arrives first,
    # then the swallowed parameter, then whatever followed it.
    marker = '</contents>\n<parameter name="reason">because</parameter>'
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

    # The fixture must REACH the guard. An earlier version had no closing tag,
    # so the evidence check refused first and the mixed-error guard was never
    # exercised — deleting it left the suite green.
    absorbed = 'body</contents>\n<parameter name="reason">r</parameter>'
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


def test_prose_about_the_syntax_gets_a_hedge_not_an_accusation():
    """The resolution of a boundary that generated THREE review rounds.

    Each round tightened the predicate so an ASSERTIVE message would be safe:
    any marker, then a closing tag anywhere, then a closing tag positioned
    before the marker. Text order is unbounded, so it never converged — and the
    ordering rule was wrong in BOTH directions (it fired on
    `a stray </parameter> before <parameter name="x">v</parameter>`, and missed a
    real absorption whose holder also discussed the syntax).

    The message was the harm, not the predicate. A caller who simply forgot an
    argument must not be told it was "sent and swallowed" — but they can be shown
    both readings and decide. That is what this pins, and it is why the predicate
    is deliberately permissive now.
    """
    prose = 'the form is <parameter name="reason">a value</parameter>, as documented'

    hint = absorbed_parameter_hint(_missing_error(content=prose), {"content": prose})

    assert hint is not None
    assert "POSSIBLY" in hint
    assert "If the markup is deliberate prose" in hint
    # The accusation the old message made, which is what actually misled.
    assert "was sent and swallowed" not in hint


def test_a_real_absorption_is_found_even_when_the_holder_discusses_the_syntax():
    """REGRESSION PIN for the false NEGATIVE the ordering rule introduced.

    With the marker-ordering rule, a holder whose prose mentioned the syntax
    BEFORE the absorbed block was skipped entirely — the first marker was the
    prose one, so the stray closing tag after it was never reached. A memory or
    follow-up *about* tool-call syntax is exactly where absorption is most
    likely, so the rule went silent on its own best case.
    """
    value = (
        'I am documenting <parameter name="reason"> usage.\n'
        "MORE TEXT</contents>\n"
        '<parameter name="work_state">ready</parameter>'
    )

    hint = absorbed_parameter_hint(_missing_error(content=value), {"content": value})

    assert hint is not None
    assert "work_state" in hint


@pytest.mark.parametrize("tag", ["</my-tag>", "</ns:tag>", "</a.b>"])
def test_a_mis_spelled_tag_with_punctuation_still_counts(tag):
    """The charset must not exclude the shapes the premise is about.

    The whole diagnosis assumes the closing tag was MIS-SPELLED. A pattern
    accepting only `[A-Za-z_][A-Za-z0-9_]*` excluded hyphens, colons and dots —
    ordinary in real markup, and exactly the sort of thing a slip produces.
    """
    value = f'text{tag}\n<parameter name="reason">why</parameter>'

    assert absorbed_parameter_hint(_missing_error(content=value), {"content": value})


def test_evidence_is_aggregated_across_every_argument():
    """REGRESSION PIN. Evidence was per-argument; the claim was per-call.

    `_absorbing_argument` returned on the FIRST holder, so a second argument's
    markup was invisible — and the message then announced that a parameter was
    "genuinely absent" while its markup sat one argument over. That is verbatim
    the defect the unevidenced-names split exists to prevent, committed one
    scope up.
    """
    a = 'A</x>\n<parameter name="reason">r</parameter>'
    b = 'B</y>\n<parameter name="work_state">w</parameter>'

    hint = absorbed_parameter_hint(_missing_error(content=a), {"content": a, "other": b})

    assert hint is not None
    assert "No such markup was found for work_state" not in hint
    assert "`content`" in hint and "`other`" in hint


def test_a_missing_argument_raised_inside_a_tool_body_is_not_diagnosed():
    """REGRESSION PIN for a fabricated explanation on a call that bound fine.

    FastMCP re-raises a ValidationError unwrapped from anywhere inside
    `tool.run`, not only from argument binding. A `missing_argument` raised by a
    helper INSIDE the tool body names parameters that are not this tool's, so
    diagnosing it invents an absorption for a call that never had one. Latent
    today — nothing in src/genesis uses `validate_call` — but it spans four
    servers and every tool added later, which is why it is checked rather than
    left as an unwritten invariant.
    """

    @validate_call
    def inner(alpha: str, beta: str) -> str:
        return alpha

    with pytest.raises(ValidationError) as excinfo:
        inner()

    absorbed = 'x</contents>\n<parameter name="alpha">v</parameter>'

    # Without the tool name there is nothing to compare against — unchanged.
    assert absorbed_parameter_hint(excinfo.value, {"content": absorbed}) is not None
    # With it, the mismatch between the error's title and the tool is the signal.
    assert absorbed_parameter_hint(excinfo.value, {"content": absorbed}, "demo_tool") is None


def test_no_argument_value_reaches_the_message():
    """These arguments carry secrets, paths and personal data, and the message
    may be logged. The only value-derived token is the trailing tag, which is
    constrained to a tag shape."""
    secret = "sk-live-DEADBEEF-and-a-private-note"
    value = f'{secret}</contents>\n<parameter name="reason">why</parameter>'

    hint = absorbed_parameter_hint(_missing_error(content=value), {"content": value})

    assert hint is not None
    assert secret not in hint
    assert "DEADBEEF" not in hint


def test_a_mis_spelled_closing_tag_is_still_recognised():
    """TRUE-POSITIVE CONTROL, and the reason the check is not `</{holder}>`.

    The commonest real cause is a MIS-SPELLED closing tag — which is precisely
    why the parser never closed the block. Testing for the holder's own name
    would miss every one of them, so the check is any closing tag before the
    marker.
    """
    absorbed = 'real content</contents>\n<parameter name="reason">why</parameter>'

    hint = absorbed_parameter_hint(_missing_error(content=absorbed), {"content": absorbed})

    assert hint is not None
    assert "reason" in hint


def test_a_model_level_missing_field_is_not_a_missing_argument():
    """The `missing_argument` filter must not accept `missing`.

    Pydantic emits `missing_argument` for a call-binding failure and `missing`
    for a required FIELD of a model. They are different failures: a nested
    model's absent field was never a tool parameter, so diagnosing it invents an
    absorption for a call that bound fine. Found by mutation — widening the
    filter to accept both survived the whole suite, and that filter is the only
    defence against the body-raised case.
    """
    from pydantic import BaseModel

    class Inner(BaseModel):
        alpha: str

    with pytest.raises(ValidationError) as excinfo:
        Inner()

    types = {e.get("type") for e in excinfo.value.errors()}
    assert "missing" in types and "missing_argument" not in types, (
        f"precondition: pydantic no longer distinguishes these ({types})"
    )
    assert missing_argument_names(excinfo.value) == []


def test_a_malformed_errors_payload_does_not_crash_the_diagnosis():
    """`errors()` is a third-party contract; a non-dict entry must not propagate.

    Found by mutation: dropping the isinstance guard survived, because nothing
    fed it an entry that was not a dict. The diagnosis runs on an already-failing
    path, so an exception escaping it would replace a real error with a crash.
    """

    class _Weird(Exception):
        def errors(self):
            return [
                {"type": "missing_argument", "loc": ("reason",)},
                "not-a-dict",
                {"type": "missing_argument", "loc": ()},
                {"type": "missing_argument", "loc": (0,)},
            ]

    assert missing_argument_names(_Weird()) == ["reason"]
