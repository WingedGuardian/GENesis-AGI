"""Failure detail for the career-outreach monitor's remote dispatches.

When the remote ``claude -p`` fails, the detail the monitor records (and job health
stores as ``last_error``) carries the remote's own diagnosis: the cause from the CLI's
result object, the exit code, the API status, the terminal reason and a stderr excerpt.
Every piece of remote text goes through the output scrubber WHOLE before any cut, so a
bound can never split a credential below what the scrubber recognises.
"""

from __future__ import annotations

import logging
import re

from genesis.security.output_scanner import (
    LOCATION_FINDINGS,
    redact_credentials,
    scan_diagnostic_controls,
    scan_outbound,
    scan_outbound_normalised,
)
from genesis.security.sanitizer import strip_control_chars

logger = logging.getLogger(__name__)

# Bound on each remote diagnostic excerpt (the error string, the CLI's result text, its
# stderr) kept in a dispatch-failure detail. Why a bound at all: the detail is joined into
# job_health's ``last_error`` (no length bound of its own), which is rendered into
# awareness signals and reflection prompts, so a remote that dumps a long error would
# flood each of them. Why 300: a CLI's own failure message is one or two lines, which fits
# with room to spare. For a CLI's output the TAIL is kept, because its last line is where
# it says why it exited (warnings come first); an error string is a message read
# start-first, so it keeps its HEAD. Either cut is declared with a marker. Location-style
# findings (``LOCATION_FINDINGS``) may stay in this owner-facing record; credentials are
# redacted in place, and anything else still flagged withholds the excerpt whole.
_EXCERPT_CHARS = 300
# ``terminal_reason`` is a closed CLI vocabulary (e.g. ``api_error``): accept the token
# shape, never free text.
_REASON_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,39}")


def scrubbed_excerpt(
    label: str, raw: object, *, labelled: bool = True, head: bool = False
) -> str | None:
    """A scrubbed, one-line, bounded excerpt of remote diagnostic text, or None when empty.

    ``labelled`` prefixes ``<label>: ``; the withheld forms always name the label.
    ``head`` keeps the start of an over-long text instead of its end.
    Fails closed: if the scrubber raises, or something it could not redact remains, the
    text is omitted and the excerpt says it was withheld."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        # Inspect original context before a redacted prefix hides a split suffix.
        ambiguous = scan_diagnostic_controls(raw) - LOCATION_FINDINGS
        if ambiguous:
            return f"{label} withheld (unredactable content: {', '.join(sorted(ambiguous))})"
        # Scrub BEFORE bounding, so a cut cannot split a credential below the
        # scrubber's length floor. Scrub once on the raw text (an assignment is redacted
        # to the end of its line, so line structure matters), then flatten, then scrub
        # and scan the FLATTENED text: flattening turns NUL / zero-width runs into a
        # space, which can join a credential or card number the raw pass never saw. The
        # scrubbed text is also scanned with those characters deleted, for a credential
        # they split (``scan_outbound_normalised``, shared with the SSH adapter).
        scrubbed, _ = redact_credentials(raw)
        text, _ = redact_credentials(strip_control_chars(scrubbed))
        leftover = (
            set(scan_outbound(text).detected) | scan_outbound_normalised(scrubbed)
        ) - LOCATION_FINDINGS
    except Exception:
        logger.warning("career outreach: %s scrubber failed; withheld", label, exc_info=True)
        return f"{label} withheld (scrubber failed)"
    if leftover:
        return f"{label} withheld (unredactable content: {', '.join(sorted(leftover))})"
    if not text:
        return None
    if len(text) > _EXCERPT_CHARS:
        omitted = len(text) - _EXCERPT_CHARS
        if head:
            text = f"{text[:_EXCERPT_CHARS]} <rest omitted: {omitted:,} chars>"
        else:
            text = f"<omitted first {omitted:,} chars> {text[-_EXCERPT_CHARS:]}"
    return f"{label}: {text}" if labelled else text


def remote_text(value: object) -> str:
    """Remote error text for a detail line: scrubbed whole, one line, head-bounded.

    For an error string or exception a remote module produced; never the raw value."""
    text = None if value is None else str(value)
    return scrubbed_excerpt("error", text, labelled=False, head=True) or "<empty error>"


def dispatch_failure_detail(result: dict, fallback: str) -> str:
    """The adapter's error plus the remote's own diagnosis, every text part scrubbed.

    A failed ``claude -p --output-format json`` puts its CAUSE in stdout's result object
    (``result``, ``api_error_status``, ``terminal_reason``), while stderr often holds
    only a tag or an unrelated warning (MEASURED on a forced exit-1 run); so both are
    kept. The adapter returns the parsed result as ``result_text`` on a non-zero exit;
    an exit-0 ``is_error`` run carries it as ``text``. The top-level ``error`` can be a
    module's exception text, so it is scrubbed like the rest."""
    error = result.get("error")
    # A non-string error (a status number, a dict) is rendered, not swapped for the fallback.
    error = None if error is None or error is False or error == "" else str(error)
    parts = [scrubbed_excerpt("error", error, labelled=False, head=True) or fallback]
    cause = result.get("result_text")
    if cause is None and result.get("is_error"):
        cause = result.get("text")
    # The cause goes right after the error: a renderer that cuts last_error short
    # (one health alarm shows 80 chars) then still shows the start of it.
    excerpt = scrubbed_excerpt("cause", cause)
    if excerpt:
        parts.append(excerpt)
    code = result.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(f"exit={code}")
    status = result.get("api_error_status")
    if isinstance(status, int) and not isinstance(status, bool):
        parts.append(f"api_status={status}")
    reason = result.get("terminal_reason")
    if isinstance(reason, str) and _REASON_TOKEN.fullmatch(reason):
        parts.append(f"reason={reason}")
    excerpt = scrubbed_excerpt("stderr", result.get("stderr"))
    if excerpt:
        parts.append(excerpt)
    return " | ".join(parts)
