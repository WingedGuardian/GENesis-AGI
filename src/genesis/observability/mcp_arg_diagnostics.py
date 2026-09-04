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
the markup for a parameter the server says is *missing*, the call was malformed,
not incomplete.

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

# Bound the scan, but from BOTH ENDS. Absorption appends the swallowed markup to
# the TAIL of the value, so a head-only window goes blind on exactly the long
# arguments this module exists for: measured, a prefix-only cap of this size
# fired at 199,000 characters and went SILENT at 200,000, while the docstring
# above and the hint's own closing advice both talk about LONG arguments. The
# cap is for pathological payloads, not for prose — full-scan cost is roughly
# 0.8 ms/MB on a call that is already failing.
_SCAN_WINDOW = 200_000
# The fingerprint needs only the last few bytes; scanning a multi-megabyte value
# to find a tag anchored at its end is pure waste.
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


def _scannable(value: str) -> str:
    """Head AND tail of a value, so tail-appended evidence is never missed."""
    if len(value) <= 2 * _SCAN_WINDOW:
        return value
    return value[:_SCAN_WINDOW] + value[-_SCAN_WINDOW:]


def _absorbing_argument(
    arguments: dict[str, Any], missing: list[str]
) -> tuple[str, list[str]] | None:
    """(argument_name, the missing names EVIDENCED inside it), else None.

    Returns every evidenced name, not the first. The caller distinguishes
    parameters it can prove were swallowed from ones it cannot, which is the
    difference between a true explanation and a confident wrong one.

    Requires the evidence to NAME a missing parameter. A bare closing tag alone
    is not enough: prose about tool-call syntax legitimately contains one, and
    this repository's own documentation does.
    """
    wanted = set(missing)
    for name, value in (arguments or {}).items():
        if not isinstance(value, str) or not value:
            continue
        found = {m for m in _PARAM_OPEN.findall(_scannable(value)) if m in wanted}
        if found:
            # Preserve pydantic's ordering rather than set order.
            return name, [m for m in missing if m in found]
    return None


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


def absorbed_parameter_hint(exc: Any, arguments: dict[str, Any]) -> str | None:
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

    hit = _absorbing_argument(arguments, missing)
    if hit is None:
        return None
    holder, evidenced = hit
    unevidenced = [m for m in missing if m not in evidenced]

    tail = _trailing_close_tag(str(arguments.get(holder, "")))
    tail_note = (
        f" That value also ends with `{tail}`, which is where the emission was cut." if tail else ""
    )
    unevidenced_note = (
        f" No markup was found for {', '.join(unevidenced)}, so "
        f"{'those' if len(unevidenced) > 1 else 'that one'} may genuinely be "
        f"absent — supply {'them' if len(unevidenced) > 1 else 'it'} explicitly."
        if unevidenced
        else ""
    )
    return (
        f"Malformed tool call — NOT a missing value. The `{holder}` argument "
        f"contains markup for {', '.join(evidenced)}, so its closing tag did not "
        f"match its opening tag and the parameters after it were absorbed into "
        f"that string.{tail_note} That means {', '.join(evidenced)} "
        f"{'were' if len(evidenced) > 1 else 'was'} sent and swallowed, so "
        f"re-sending the same call unchanged will fail identically."
        f"{unevidenced_note} Re-send it with each parameter in its own block, "
        f"and check that every closing tag matches the tag it opened. If "
        f"`{holder}` is long, shortening it makes the slip less likely to recur."
    )
