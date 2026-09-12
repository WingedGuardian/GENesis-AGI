"""Turn a misleading "missing argument" error into the real cause.

A tool call whose long free-text argument is emitted with a closing tag that
does not match its opening tag silently swallows every parameter declared AFTER
it into that string. The server then reports those parameters as **missing** —
which reads like the caller forgot them, or like a tool bug, when in fact they
were sent and absorbed. Measured cost of that misreading on this install: six
consecutive identical refusals of one ``follow_up_create`` call before the shape
was diagnosed, with the answer sitting in the error's own ``input_value`` the
whole time.

The diagnosis is cheap and mechanical: if a *provided* argument's text contains
the markup for a parameter the server says is *missing*, that is worth telling
the caller.

It is worth telling them as a POSSIBILITY, not as a verdict, and that distinction
is the whole design. The same text appears when a caller legitimately writes
*about* tool-call syntax and simply forgot an argument — the two are
indistinguishable from the arguments alone. Three review rounds went into
tightening the predicate so an assertive message would be safe (any marker, then
a closing tag, then a closing tag positioned before the marker) and it was wrong
in both directions every time, because text order is unbounded.

So the message hedges and offers both readings. That makes a false positive a
suggestion the caller dismisses in one read, and a false negative merely a missed
nicety — which is what allows the predicate to be permissive rather than forcing
a fourth ordering rule.

SAFETY PROPERTY, and it is the reason this is worth doing: this module only ever
runs on a call that is ALREADY failing validation. It cannot make a well-formed
call fail, and it never alters arguments — it replaces an explanation. The worst
case for a false positive is a slightly wrong explanation on a call that was
going to be refused regardless.

Deliberately NOT here: recovering the swallowed values and completing the call.
That means re-parsing, with a second parser, the exact input the first parser
could not read — and writing the result into permanent record. A wrong row is
worse than a missing one, especially where a recovered value picks a lane
(``work_state``) or truncates prose. The remedy is to re-send the call
correctly, which a caller who is told the real cause does on the first retry.

REACHABILITY DEPENDS ON A LIBRARY DEFAULT, so it is recorded rather than
assumed: FastMCP's ``strict_input_validation`` defaults to False, which is why
argument binding raises inside the tool call and this middleware sees it. With
``FASTMCP_STRICT_INPUT_VALIDATION=true`` the MCP lowlevel server runs a
jsonschema check BEFORE any middleware, and this diagnosis never runs at all.
That dependency is pinned by a test rather than left to be rediscovered.

This recognises ONE client's serialization syntax — the ``<parameter name="…">``
form. That is deliberate: it diagnoses a specific known emitter, not tool calls
in general. A different client's markup needs its own pattern here.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["absorbed_parameter_hint", "missing_argument_names"]

# `<parameter name="x">` in either quoting style. This is the strong signal —
# it NAMES a parameter — and it is the only thing that ever triggers the hint.
_PARAM_OPEN = re.compile(r"""<parameter\s+name\s*=\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")
# A bare closing tag at the very END of a value: the truncation fingerprint.
_TRAILING_CLOSE = re.compile(r"</[A-Za-z_][A-Za-z0-9_]*>$")
# Any closing tag. Deliberately broad — hyphens, colons and dots all appear in
# real markup, and the premise of the whole diagnosis is that the tag was
# MIS-SPELLED, so a narrow charset excludes exactly the cases it should catch.
_ANY_CLOSE = re.compile(r"</[^\s<>/]+>")

# NO scan window. There were two, and both were wrong in ways that only showed up
# under review:
#   * a HEAD-only cap went blind on long values, because absorption appends to
#     the TAIL — it fired at 199,000 characters and went silent at 200,000, while
#     this module's whole subject is LONG arguments;
#   * splicing head+tail to fix that could FABRICATE a marker spanning the join
#     that existed in neither half, and DROPPED a marker sitting in the middle of
#     a value longer than twice the window (verified: a real marker between two
#     200k runs disappeared).
# Both are artifacts of a bound that no measurement ever justified. A full scan
# costs roughly 0.8 ms/MB, on a call that has ALREADY failed validation, and
# `finditer` does not copy the string. Scanning everything is cheaper than being
# wrong in two directions.
#
# The fingerprint below is the one place a bound is still correct: the tag is
# anchored at the very end, the slice is a SUFFIX (so no join is created), and
# the `$` anchor means a tag clipped by the window start cannot match.
_TAIL_WINDOW = 4_096


def missing_argument_names(exc: Any) -> list[str]:
    """Names pydantic reported as missing, in order. Empty for any other error.

    Reads the structured ``errors()`` rather than the rendered message: the
    message is human prose that changes between pydantic versions, while the
    ``missing_argument`` type and the ``loc`` tuple are the stable contract.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return []
    try:
        entries = errors()
    except Exception:
        return []
    names: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "missing_argument":
            continue
        loc = entry.get("loc") or ()
        if loc and isinstance(loc[-1], str):
            names.append(loc[-1])
    return names


def _total_error_count(exc: Any) -> int:
    """How many validation entries the error carries, of any type.

    Used to refuse the substitution when the error is MIXED. Returns -1 when the
    count cannot be read, which never equals the missing count and so also
    refuses — the safe direction for an uncertain read.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return -1
    try:
        return len(errors() or [])
    except Exception:
        return -1


def _absorbing_arguments(
    arguments: dict[str, Any], missing: list[str]
) -> tuple[list[str], list[str]]:
    """(every holder carrying markup, every missing name evidenced across ALL).

    AGGREGATED, not first-hit. The caller makes a claim about the CALL — which
    parameters look swallowed and which look genuinely absent — so the evidence
    behind it has to be gathered from every argument first. Returning on the
    first holder meant a second argument's markup was invisible, and the message
    then announced that a parameter was "genuinely absent" while its markup sat
    one argument over. That is the same defect the unevidenced-names split was
    added to prevent, committed one scope up.
    """
    wanted = set(missing)
    holders: list[str] = []
    found: set[str] = set()
    for name, value in (arguments or {}).items():
        if not isinstance(value, str) or not value:
            continue
        # finditer over the WHOLE value — no slice, so no join to fabricate a
        # marker across and no middle to drop.
        first_marker = _PARAM_OPEN.search(value)
        if first_marker is None:
            continue
        # A closing tag somewhere in the value, which is what an emission that
        # failed to close leaves behind. DELIBERATELY not an ordering rule any
        # more, and that reversal is the point of this revision.
        #
        # Three rounds went into tightening this predicate so an ASSERTIVE
        # message would be safe: first any marker, then a closing tag anywhere,
        # then a closing tag BEFORE the marker. Text order is unbounded, so it
        # was never going to converge — and the ordering rule was wrong in both
        # directions. `a stray </parameter> before <parameter name="x">v</...>`
        # fired on prose, while a REAL absorption whose holder also discusses the
        # syntax was missed, because the prose marker came first. A memory or
        # follow-up about tool-call syntax is precisely where absorption is most
        # likely and where it went silent.
        #
        # The message is the harm, not the predicate. It now HEDGES, so a false
        # positive is a suggestion the caller can dismiss in one read and a false
        # negative costs only a missed nicety. That is what makes a permissive
        # predicate the right choice rather than a lax one.
        if _ANY_CLOSE.search(value) is None:
            continue
        hit = {m.group(1) for m in _PARAM_OPEN.finditer(value)} & wanted
        if hit:
            holders.append(name)
            found |= hit
    # Preserve pydantic's ordering rather than set order.
    return holders, [m for m in missing if m in found]


def _trailing_close_tag(value: str) -> str | None:
    """The closing tag a value ENDS with, which is the truncation fingerprint.

    Anchored to the end and scanned over the tail only. A first version searched
    a window and took the FIRST match, so a value ending ``…</parameter>\\n</domain>``
    reported nothing: the search found ``</parameter>``, the endswith check
    failed against it, and the genuine fingerprint one tag later was never
    considered.
    """
    return (
        match.group(0)
        if (match := _TRAILING_CLOSE.search(value[-_TAIL_WINDOW:].rstrip()))
        else None
    )


def absorbed_parameter_hint(
    exc: Any, arguments: dict[str, Any], tool_name: str | None = None
) -> str | None:
    """A replacement error message, or None to leave the original alone.

    None is the default on every uncertain path — an unrecognised error, no
    missing arguments, or no provided value naming one of them. Returning a
    confident wrong explanation would be worse than the vague true one, and that
    rule binds the MESSAGE too: a parameter with no evidence behind it is
    reported as possibly genuinely absent, never asserted to have been swallowed.
    """
    missing = missing_argument_names(exc)
    if not missing:
        return None
    # A `missing_argument` can also be raised INSIDE a tool body — FastMCP
    # re-raises a ValidationError unwrapped from anywhere in `tool.run`, not just
    # from argument binding. Those names are not this tool's parameters, so
    # diagnosing them produces an entirely fabricated explanation for a call that
    # bound perfectly. Pydantic titles a binding failure `call[<tool>]`; anything
    # else came from deeper in. Latent today (nothing in src/genesis uses
    # `validate_call`) but it spans four servers and every tool added later, so
    # it is checked rather than left as an unwritten invariant.
    if tool_name is not None:
        title = getattr(exc, "title", None)
        if title not in (tool_name, f"call[{tool_name}]"):
            return None
    # A ValidationError can carry a missing_argument AND an independent error —
    # a wrong type on some other argument. Replacing the whole exception would
    # discard that second problem, and the caller could not fix everything on the
    # "first retry" this feature exists to enable. So the replacement is offered
    # only when EVERY entry is a diagnosed missing argument; otherwise the
    # original error, which names all of them, is the more useful one.
    if _total_error_count(exc) != len(missing):
        return None

    holders, evidenced = _absorbing_arguments(arguments, missing)
    if not holders or not evidenced:
        return None
    holder = holders[0]
    unevidenced = [m for m in missing if m not in evidenced]

    tail = _trailing_close_tag(str(arguments.get(holder, "")))
    tail_note = (
        f" That value also ends with `{tail}`, which is where the emission was cut." if tail else ""
    )
    unevidenced_note = (
        f" No such markup was found for {', '.join(unevidenced)}, so "
        f"{'those' if len(unevidenced) > 1 else 'that one'} is more likely "
        f"genuinely absent."
        if unevidenced
        else ""
    )
    where = "`" + "`, `".join(holders) + "`"
    return (
        f"POSSIBLY a malformed tool call rather than a missing value — check "
        f"before re-sending. {where} contains parameter markup naming "
        f"{', '.join(evidenced)}.{tail_note}{unevidenced_note}\n\n"
        f"If that markup was meant as a separate parameter block, then a closing "
        f"tag did not match its opening tag, everything after it was absorbed "
        f"into that string, and re-sending the call unchanged will fail "
        f"identically — re-send with each parameter in its own block and check "
        f"that every closing tag matches the tag it opened.\n\n"
        f"If the markup is deliberate prose, then {', '.join(missing)} really "
        f"{'are' if len(missing) > 1 else 'is'} missing and should be supplied."
    )
