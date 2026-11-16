"""Tool-call outcome rows from a parsed transcript spine (issue #4712).

``scripts/edit_failure_sensor.py`` records Edit and Write outcomes at Stop and
SessionEnd. Every other tool call's result lives only in the transcript. The
extraction job already parses each foreground transcript into an action spine
(``learning/procedural/struggle_detector.build_spine_and_haystack``); this turns
that spine into ``tool_call_outcomes`` rows so failures of every tool can be
counted and their recurrence measured.

What is written, and what deliberately is not:

* One row per main-session tool call that HAS a result. A call with no result
  yet is skipped rather than recorded as a failure: the spine marks it ``error``
  for the struggle score, but it is usually still running, and ``INSERT OR
  IGNORE`` would freeze the wrong outcome forever. It is recorded on a later
  cycle once its result is in the transcript.
* Sidechain (subagent) records are skipped, as the Stop-hook sensor skips them.
* Edit and Write are skipped: they stay the Stop-hook sensor's alone. Its
  rows from before #1597 carry a NULL ``tool_use_id``, so they cannot dedupe
  against this writer, and re-parsing a resumed session's history would count
  those calls twice (measured: 14,892 such rows, 2026-05-18 to 2026-09-10).
  One writer per tool also leaves no case where the two could disagree.
* ``success`` comes from the shared transcript classifier
  (``transcript_analytics.classify``), so a flagged error, a hook refusal and a
  user rejection all count as failures, exactly as the sensor counts them.
* ``error_snippet`` is the failure text passed through the shared scrubber
  (``transcript_analytics.scrub.scrub_text``) and only then cut, so a cut never
  lands inside a credential the scrubber would have recognised whole. When the
  scrubber fails or withholds the text, the snippet is NULL and the failure is
  still recorded. Text over the parser's bound is withheld there, not cut.
* This writer does not rebuild a call older than the prune's row horizon, and
  drops its failure text past the snippet horizon. A resumed session replays its
  whole transcript, and ``INSERT OR IGNORE`` dedupes only against rows that
  still exist, so without this a pruned call would come back until the next
  prune. The Stop-hook sensor applies no such cutoff to its Edit/Write rows.
* Outcomes are recorded after the extraction watermark moves; a failed record
  is logged and not retried, so a session that has ended loses those calls.

The kind of a failure is not stored yet, and it cannot be recomputed from the
row: the classifier reads the full result text and the record's
``toolDenialKind``, while the row keeps neither (the snippet is 400 scrubbed
characters, and a hook prefix can close past character 1,500). Storing the
class at write time needs its own schema change.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# Stored characters of scrubbed failure text: the classifier's own head window
# (``transcript_analytics.classify._HEAD``). Enough to read the reason's first
# sentence; not enough to re-derive the class (see the module docstring).
SNIPPET_CHARS = 400

# A JSON ``\uXXXX`` escape. The scrubber decodes escapes only inside a JSON
# string that parses; inside an unterminated quote it falls back to plain text
# and an escaped credential passes through. Measured: 'token=\u0073k-ant-…'
# after an unclosed quote came out unredacted.
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")

# Tools ``scripts/edit_failure_sensor.py`` records; this writer leaves them to it.
SENSOR_TOOLS = frozenset({"Edit", "Write"})

_scrub_unavailable_logged = False


def _parse_ts(value: object) -> datetime | None:
    """A transcript timestamp as an aware UTC datetime, or None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _snippet(text: str | None) -> str | None:
    global _scrub_unavailable_logged
    if not text:
        return None
    from genesis.transcript_analytics import scrub
    from genesis.transcript_analytics.scrub import scrub_text

    if not scrub.available() and not _scrub_unavailable_logged:
        # Fail-closed (every snippet is withheld), but say so once rather than
        # leaving a table of NULL snippets with no explanation.
        logger.warning("tool-outcome scrubber unavailable; failure text will not be stored")
        _scrub_unavailable_logged = True
    try:
        scrubbed, failed = scrub_text(text)
    except Exception:  # noqa: BLE001 - a scrubber fault withholds text, never raises
        logger.warning("tool-outcome scrub raised; storing no failure text", exc_info=True)
        return None
    if failed or not scrubbed:
        return None
    if _UNICODE_ESCAPE.search(scrubbed):
        # Fail closed on an escape the scrubber did not decode: decode a copy,
        # scrub it, and store nothing if that copy held anything to redact.
        decoded = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), scrubbed)
        try:
            again, again_failed = scrub_text(decoded)
        except Exception:  # noqa: BLE001 - as above
            return None
        if again_failed or again != decoded:
            return None
    return scrubbed[:SNIPPET_CHARS]


def outcome_rows(spine: list[dict], *, fallback_session_id: str | None) -> list[tuple]:
    """``tool_call_outcomes`` rows for every recordable call in ``spine``.

    Row order matches ``db/crud/tool_call_outcomes.insert_outcomes``:
    ``(session_id, tool_name, file_path, success, error_snippet, timestamp,
    tool_use_id)``.
    """
    from genesis.db.crud.tool_call_outcomes import (
        ROW_RETENTION_DAYS,
        SNIPPET_RETENTION_DAYS,
    )

    rows: list[tuple] = []
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    row_cut = now_dt - timedelta(days=ROW_RETENTION_DAYS)
    text_cut = now_dt - timedelta(days=SNIPPET_RETENTION_DAYS)
    for entry in spine:
        if entry.get("type") != "tool" or not entry.get("has_result"):
            continue
        if entry.get("is_sidechain") or not entry.get("tool_use_id"):
            continue
        if entry.get("tool") in SENSOR_TOOLS:
            continue  # module docstring: the Stop-hook sensor's tools
        when = _parse_ts(entry.get("timestamp"))
        if when is not None and when < row_cut:
            continue  # past the prune's horizon: do not resurrect it
        failed = entry.get("failure_class") is not None
        keep_text = failed and (when is None or when >= text_cut)
        rows.append(
            (
                entry.get("session_id") or fallback_session_id,
                entry.get("tool") or "unknown",
                entry.get("file_path"),
                0 if failed else 1,
                _snippet(entry.get("failure_text")) if keep_text else None,
                # An unparseable stamp would never match the prune's datetime(),
                # so the row (and its text) would outlive every horizon.
                entry["timestamp"] if when is not None else now,
                entry["tool_use_id"],
            )
        )
    return rows
