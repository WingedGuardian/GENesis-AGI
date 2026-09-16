"""Step 1.5 — Build an InteractionSummary from a CCOutput."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from genesis.cc.types import CCOutput
from genesis.learning.response_context import elision_marker
from genesis.learning.types import InteractionSummary

# The REQUEST gets the same treatment as the response, and for the same reason.
# It was a bare 500-char prefix — the exact shape `_fit`'s docstring forbids —
# handed to the delta assessor, whose job is judging request against delivery.
# A request cut mid-sentence reads as an underspecified request. MEASURED on
# this install's own traffic: 222 of 1481 inbound messages (15.0%) exceed 500
# characters, the longest 5,924; `inbox/monitor.py` passes whole-file content
# and `mail/monitor.py` a joined subject list, neither bounded by a messaging
# limit. So this is a safety valve above real traffic, not a working limit.
#
# COUNTED IN BYTES, not characters, and that is the whole point of the unit.
# A character cap bounds nothing a model cares about: 20,000 characters of CJK
# or emoji is up to 80,000 UTF-8 bytes, and the two independent caps then had no
# combined bound at all. A BPE token is at least one byte, so N bytes can never
# be more than N tokens — a byte valve is a PROOF about the prompt's size rather
# than an estimate from one alphabet. Both valves together therefore admit at
# most 40,000 tokens of payload, against the 128k smallest context in the
# graders' chains, leaving the instructions and calibration ample room.
# For ASCII — the overwhelming majority of real traffic — bytes and characters
# are the same number, so nothing about the common case changes.
_MAX_USER_TEXT = 20_000

# A SAFETY VALVE against a pathological payload, not a working limit. It has to
# clear real traffic with room to spare, because a response that reaches a
# grader in fragments reads exactly like a response the model abandoned — and
# the grader's verdict is written to permanent memory. MEASURED over 30 days of
# outbound TELEGRAM traffic (n=854): mean 1036 chars, max 6006, none above 20k —
# so on that channel this valve never opens. `build_summary` is also reached from
# terminal/dashboard (`cc/conversation.py`), inbox and mail, whose responses are
# NOT bounded by a messaging limit; those are the cases the elision path exists
# for. In BYTES, for the reason given at `_MAX_USER_TEXT`: 20,000 bytes is at
# most 20,000 tokens whatever alphabet the payload uses, where 20,000
# CHARACTERS was only "roughly 5k tokens" for ASCII and unbounded otherwise.
_MAX_RESPONSE_TEXT = 20_000

# Kept for the elided case: the ending is the half a grader needs to judge
# whether generation stopped early, so the tail is preserved and the MIDDLE is
# dropped. Head is generous enough to carry the response's shape. In BYTES, same
# unit as the valves — mixing the two would let a multi-byte head overrun a
# byte-counted valve.
_ELIDE_HEAD = 12_000
_ELIDE_TAIL = 4_000

# Without this the elided COUNT goes negative just past the valve, and the
# marker would report a nonsense figure. Asserted rather than commented because
# the failure is silent and only shows up in a rendered prompt.
if min(_MAX_RESPONSE_TEXT, _MAX_USER_TEXT) <= _ELIDE_HEAD + _ELIDE_TAIL:
    raise ValueError(  # pragma: no cover - config guard
        "elide head+tail must stay under BOTH valves, or the elided count in "
        "the marker goes negative just past the smaller one"
    )

# Patterns that indicate tool usage in CC output.
_TOOL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Tool:\s*(\w+)"),
    re.compile(r"<tool_call>\s*(\w+)"),
    re.compile(r"Using tool:\s*(\w+)"),
]


def _extract_tool_calls(text: str) -> list[str]:
    """Return deduplicated tool names found in *text*, preserving first-seen order.

    FALLBACK ONLY. This reads the response body, so it cannot tell a tool that
    RAN from one the response merely discussed — a reply that says "I would run
    Tool: Bash" yields ``["Bash"]``. That matters twice over: the name reaches
    the graders as an authoritative line OUTSIDE the response fence, and
    ``prefilter.should_skip`` treats a non-empty list as evidence the
    interaction is substantive. Prefer ``CCOutput.tools_used``, which the
    runtime observes and the response cannot write; this stays for the
    invocations that never streamed and for hand-built ``CCOutput``s, where the
    alternative is no signal at all.
    """
    seen: set[str] = set()
    result: list[str] = []
    for pat in _TOOL_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _encodable(text: str) -> str:
    """Return *text* with anything UTF-8 cannot encode rendered as an escape.

    An unpaired surrogate is a `str` Python accepts and UTF-8 rejects:
    `json.loads('"\\ud800"')` yields one, so it arrives from any JSON source —
    an inbound request, a decoded CC stream result. Every measurement below is
    in bytes, so the first `.encode()` raised `UnicodeEncodeError` straight out
    of `build_summary`, killing retrospective grading and every downstream
    learning write for that interaction. The SHORT path raised too: the valve
    was never the trigger, encoding was.

    `backslashreplace` rather than `replace`: the point of this pipeline is that
    a grader is shown what actually happened, so a code point that cannot be
    encoded is rendered as `\\ud800` — visible, still valid UTF-8 — instead of
    being silently swapped for `?`. It runs BEFORE any measuring, so the byte
    valve and the reported character count both describe the text the graders
    are actually handed.

    SCOPE, stated exactly, because the obvious stronger claim is false. This
    covers the three FREE-TEXT fields a grader prompt renders: `user_text` and
    `response_text` (via `_fit`) and the `tool_calls` names (in `build_summary`).
    It does NOT cover `session_id`, which reaches the same prompts and has the
    same CC-JSON-stream provenance — deliberately, because that field is an
    IDENTIFIER used as a correlation key, `_encodable` is non-injective (a real
    surrogate and a literally-typed `\\ud800` collapse together), and silently
    rewriting a key to protect a render path is how two identities become one.
    A session id that is not encodable is malformed rather than long, and the
    honest handling is to reject it upstream where it is minted — not to mutate
    it here. Residual, and named so the next reader does not have to rediscover
    it: such an id would still raise at render time.
    """
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _fit(text: str, cap: int) -> tuple[str, int]:
    """Return (text, characters removed) — whole, or head + marker + tail.

    ONE mechanism for both texts the graders are shown. Never a bare prefix:
    text that simply stops mid-word is indistinguishable from text that was
    abandoned, which is the exact misreading this function exists to prevent —
    so if anything is removed, what remains must SAY so and must keep its
    ending. That applies to the request as much as the reply; a truncated
    request reads as an underspecified one to the assessor grading scope.

    The COUNT is returned rather than left for a reader to recover from the
    marker. What this pipeline removed is a fact the pipeline holds; searching
    the returned text for it hands that decision to the text being graded.

    MEASURED IN BYTES, REPORTED IN CHARACTERS, deliberately. The valve is a
    bound on the prompt the model has to hold, and only bytes bound that (see
    `_MAX_USER_TEXT`). The marker speaks to a grader reading prose, for whom
    "characters" is the meaningful unit — and it must stay the unit the count
    is computed in, or the marker reports a number that does not describe the
    text beside it. Slicing on the encoded form can land mid-codepoint; the
    partial bytes at that boundary are dropped rather than replaced, so the
    fitted text is always valid and the reported count stays exact by being
    derived from what SURVIVED, never from the requested sizes.
    """
    text = _encodable(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text, 0
    head = encoded[:_ELIDE_HEAD].decode("utf-8", "ignore")
    tail = encoded[-_ELIDE_TAIL:].decode("utf-8", "ignore")
    elided = len(text) - len(head) - len(tail)
    fitted = f"{head}\n\n{elision_marker(elided)}\n\n{tail}"
    return fitted, elided


def _fit_response(text: str) -> tuple[str, int]:
    """`_fit` at the response valve."""
    return _fit(text, _MAX_RESPONSE_TEXT)


def build_summary(
    output: CCOutput,
    session_id: str,
    user_text: str,
    channel: str,
) -> InteractionSummary:
    """Create an :class:`InteractionSummary` from raw interaction data."""
    response_text, elided_chars = _fit_response(output.text)
    fitted_user_text, user_elided_chars = _fit(user_text, _MAX_USER_TEXT)
    return InteractionSummary(
        session_id=session_id,
        user_text=fitted_user_text,
        response_text=response_text,
        response_truncated=output.bg_truncated,
        response_elided_chars=elided_chars,
        # `_encodable` here as well as in `_fit`, so the chokepoint claim covers
        # the WHOLE summary rather than just its two long texts. The scrape
        # cannot produce an unencodable name (`\w` does not match a surrogate —
        # verified), but `tools_used` is read from CC's JSON stream, and
        # `json.loads` accepts `"\ud800"`. That name would then reach a grader
        # prompt OUTSIDE `_fit`, and the crash would land at render time with
        # nothing pointing back here.
        tool_calls=[
            _encodable(name)
            for name in (
                list(output.tools_used)
                if output.tools_used is not None
                else _extract_tool_calls(output.text)
            )
        ],
        # None vs () is the whole distinction: no runtime REPORT, versus a
        # runtime report of zero tools. Collapsing them makes "Tools used: none"
        # an assertion on every non-streaming interaction, where the pipeline
        # in fact knows nothing.
        tool_calls_from_runtime=output.tools_used is not None,
        user_text_elided_chars=user_elided_chars,
        token_count=output.input_tokens + output.output_tokens,
        channel=channel,
        timestamp=datetime.now(UTC),
    )
