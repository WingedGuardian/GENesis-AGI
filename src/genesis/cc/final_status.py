"""A background session's own account of how it ended.

A session that was denied a tool, or could not get what it needed, usually
still exits cleanly, so the process alone records it as a success. The model
knows better. ``CCInvocation.report_status=True`` adds :data:`CONTRACT` to the
session's system prompt, asking it to end its final message with one tagged
block; the invoker then splits that block off the output (:func:`split`), so
every caller's own parser reads the same text it read before the contract
existed. Callers that own a ``cc_sessions`` row turn the report into an outcome
with :func:`outcome_of`; a mechanical failure (an error, a truncated run) always
wins over what the model said.

The block is matched only where it ENDS the text. A ``final_status`` block
followed by anything else is left in place and the run counts as unreported:
stripping text from the middle of an answer could change what a caller parses,
and a session can legitimately quote such a block while it works.

Stdlib only: ``cc/types.py`` imports :class:`FinalStatus` from here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: Statuses the model may report.
REPORTED = ("completed", "blocked", "failed")

#: Status recorded when a tagged block ends the text but does not parse.
MALFORMED = "malformed"

#: Longest reason kept; a reason is meant to be one sentence.
REASON_MAX_CHARS = 500

CONTRACT = """\
When you are done, end your final message with this block, after everything \
else, including any JSON you were asked to produce. It is removed before your \
output is read, so it never changes what you were asked to return.

```final_status
{"status": "completed", "reason": "one sentence"}
```

status is one of:
- "completed": you did what was asked.
- "blocked": something stopped you from finishing: a tool or access was denied, \
something you needed was missing, or only the owner can decide the next step.
- "failed": you tried and it did not work.
reason says what happened, in one sentence."""

# The tagged block: it starts a line and ends the text. The body is non-greedy
# and may not contain a fence, so the match is always the LAST block. Accepted
# residual: a model that leaves its own ```json fence unclosed before the block
# loses the fence our tag was standing in for; parsers with a brace fallback
# (ego, campaign) still read it.
_TRAILER = re.compile(r"(?:\A|\n)```final_status[ \t]*\n((?:(?!```)[\s\S])*?)\n?```\s*\Z")


@dataclass(frozen=True)
class FinalStatus:
    """One parsed report. ``status`` is in :data:`REPORTED` or :data:`MALFORMED`."""

    status: str
    reason: str = ""


def split(text: str) -> tuple[str, FinalStatus | None]:
    """Return ``text`` without a trailing ``final_status`` block, and the report.

    No trailing block: ``(text, None)``, text unchanged. A trailing block that is
    not a JSON object with a known status is still removed (it is ours, not the
    caller's) and reported as :data:`MALFORMED`.
    """
    if not text:
        return text, None
    match = _TRAILER.search(text)
    if match is None:
        return text, None
    rest = text[: match.start()].rstrip()
    try:
        data = json.loads(match.group(1))
    except (ValueError, RecursionError):  # JSONDecodeError is a ValueError
        return rest, FinalStatus(MALFORMED)
    if not isinstance(data, dict):
        return rest, FinalStatus(MALFORMED)
    status = data.get("status")
    if status not in REPORTED:
        return rest, FinalStatus(MALFORMED)
    reason = data.get("reason")
    reason = reason.strip()[:REASON_MAX_CHARS] if isinstance(reason, str) else ""
    return rest, FinalStatus(status, reason)


_OUTCOME = {"completed": "finished", "blocked": "blocked", "failed": "failed"}


def outcome_of(report: FinalStatus | None) -> tuple[str | None, str | None]:
    """The ``cc_sessions`` outcome and reason a report implies.

    ``(None, None)`` when there is no report: the recorder then writes its
    default for the status (``unreported`` for a completed run). Anything that
    is not a :class:`FinalStatus` counts as no report: recording the outcome
    must never be what fails a run that otherwise finished.
    """
    if not isinstance(report, FinalStatus) or report.status not in (*REPORTED, MALFORMED):
        return None, None
    if report.status == MALFORMED:
        return "unreported", "malformed_final_status"
    return _OUTCOME[report.status], report.reason or None
