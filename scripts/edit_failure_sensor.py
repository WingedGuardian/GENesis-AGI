#!/usr/bin/env python3
"""Stop hook: record Edit/Write tool-call outcomes from the session transcript.

Writes to ``tool_call_outcomes`` — the source for the WS-2 tool-call calibration
base-rate lane (``db/crud/tool_call_outcomes.py::aggregate_success_rates`` →
``ledger/cells.py``).

Why the transcript, not PostToolUse: on current Claude Code a FAILED Edit fires
NO PostToolUse/PostToolUseFailure hook at all (MEASURED 2026-09-02, CC 2.1.246 —
issue #1597), so the old PostToolUse-based sensor recorded 0 failures in 25k rows
over ~8 weeks (its own #955 regression marker tripped). Failures — and successes —
ARE both recorded in the session transcript. On the **Stop** event we scan the
transcript (path in ``transcript_path``) and record EVERY Edit/Write tool call:
``success=0`` when its ``tool_result`` has ``is_error`` truthy, ``success=1``
otherwise. Recording both from ONE source keeps success and failure on the same
population (no base-rate skew); v1 is MAIN-SESSION ONLY (``isSidechain`` records —
sub-agents — are skipped on both).

Idempotent: Stop re-fires every turn over a growing transcript, so rows are deduped
by the globally-unique ``tool_use_id`` (``toolu_…``) via ``INSERT OR IGNORE``. The
pre-#1597 rows carry NULL ``tool_use_id`` (SQLite allows multiple NULLs in a UNIQUE
index); every row this writes carries a value.

This REPLACES both the PostToolUse success registration and the dead
PostToolUseFailure registration for this sensor (see .claude/settings.json).

Reads stdin JSON per the hook contract. Stdlib-only (runs outside the server),
fail-open (never raises, never blocks a tool call). ``GENESIS_DB_PATH`` override is
for tests/verification only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# GENESIS_DB_PATH override exists for tests/verification only; production
# hooks rely on the default. Keep this script stdlib-only (no genesis imports).
_DB_PATH = Path(
    os.environ.get("GENESIS_DB_PATH", "")
    or Path.home() / "genesis" / "data" / "genesis.db"
).expanduser()  # honor ~/... overrides like genesis.env.genesis_db_path()

# Transcripts can grow large; cap the scan to the tail to bound the Stop-hook
# budget. Stop fires per-turn, so a turn's outcomes are scanned that same turn —
# long before the tail window could roll past them in steady state.
_MAX_SCAN_BYTES = 25_000_000

# Events that deliver a transcript to scan. Registered on Stop; SessionEnd is
# accepted too (it also carries transcript_path) as a defensive fallback.
_SCAN_EVENTS = ("Stop", "SessionEnd")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        _process(json.loads(raw))
    except Exception:
        # Hooks must never crash or block a tool call.
        return


def _process(data: dict) -> None:
    if isinstance(data, dict) and data.get("hook_event_name") in _SCAN_EVENTS:
        _scan_transcript_outcomes(data)


# Failure phrasings that a NOMINALLY successful tool_result can carry. The
# predecessor sensor keyed on these because it had no `is_error` flag to read at
# all; dropping them entirely made the new sensor's correctness depend on one
# payload field never changing shape — which is the exact class of blindness
# #1597 was (the hook that never fired). Kept as a fallback, not a primary.
#
# MEASURED over this install's 1,463 transcripts, 13,135 paired Edit/Write
# results, BOTH directions:
#   * of the 373 results carrying is_error=True, 138 also match a marker — so
#     the markers are a SUBSET of the flag, never a replacement for it;
#   * of the 12,762 results WITHOUT is_error, 0 match any marker.
# So on real traffic this fallback changes nothing at all. That is the point:
# it costs zero false positives today and it is what stands between the sensor
# and a future payload that reports a soft failure in prose while omitting the
# flag.
#
# The predecessor's bare "Error:" marker is DELIBERATELY NOT carried over. It
# also measured 0/12,762 here, so it buys nothing — and its risk profile is
# different from the others: a SUCCESSFUL Edit's result contains a snippet of
# the edited file, so "Error:" appearing in ordinary source or log text would
# classify a successful edit as a failure. A specific phrasing cannot.
_SOFT_FAILURE_MARKERS = (
    "old_string not found",
    "string to replace not found",
    "not unique in the file",
    "no changes were made",
    "old_string and new_string are the same",
)


def _is_error(value: object) -> bool:
    """True iff a tool_result's EXPLICIT flag marks a failure.

    Successful results OMIT is_error (verified: 941/1023 real Edit/Write results
    had no is_error key). Accept the JSON bool plus 1/"true" so a payload-format
    drift can't silently re-blind the failure path (the exact class #1597 fixed);
    everything else is a success as far as the FLAG is concerned — see
    ``_result_failed`` for the content fallback.
    """
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() == "true")


def _result_failed(item: dict, rec: dict) -> bool:
    """Whether a tool_result reports a failure, by flag OR by content.

    The flag decides when it is present. When it is absent, a known soft-failure
    phrasing in the result text still counts — the call did not do what it was
    asked to do, and recording it as a success inflates the calibration base rate
    for exactly the payload shape the flag stops describing (Codex P2, PR #1616).
    """
    if _is_error(item.get("is_error")):
        return True
    text = _error_text(item, rec).lower()
    return any(marker in text for marker in _SOFT_FAILURE_MARKERS)


def _error_text(item: dict, rec: dict) -> str:
    """Best-effort failure text from a tool_result block (200-char capped by caller)."""
    content = item.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        joined = " ".join(p for p in parts if p).strip()
        if joined:
            return joined
    mirror = rec.get("toolUseResult")  # top-level string mirror on the transcript record
    if isinstance(mirror, str) and mirror.strip():
        return mirror
    return "tool failed (no error text)"


# How far back the recovery pass will hunt for a record boundary. A JSONL
# record can be arbitrarily long, so "seek to an offset and read a line" can land
# inside one and throw away the very record being recovered — the same defect the
# main window has, one level down. Bounded because this is a repair path on an
# advisory sensor, not a reason to read an unbounded file.
_BOUNDARY_LOOKBACK_BYTES = 4_000_000


def _line_start_at_or_before(path: Path, offset: int) -> int | None:
    """The start of the record containing ``offset``, or None if not found near it.

    Returns 0 immediately for offset 0 (the file start IS a record start). Reads
    backwards in binary so the answer is a byte offset the text reader can seek
    to, and gives up rather than guessing when no newline appears within
    ``_BOUNDARY_LOOKBACK_BYTES`` — starting mid-record would silently drop the
    record the caller came here for.
    """
    if offset <= 0:
        return 0
    probe = max(0, offset - _BOUNDARY_LOOKBACK_BYTES)
    try:
        with path.open("rb") as fh:
            fh.seek(probe)
            chunk = fh.read(offset - probe)
    except OSError:
        return None
    idx = chunk.rfind(b"\n")
    if idx == -1:
        return 0 if probe == 0 else None
    return probe + idx + 1


def _recover_earlier_tool_uses(
    path: Path,
    size: int,
    wanted: set[str],
    edits: dict[str, tuple[str, str | None]],
) -> None:
    """Fill ``edits`` for ``wanted`` ids from the window BEFORE the scan cutoff.

    Reads at most one further ``_MAX_SCAN_BYTES``, scanning only for Edit/Write
    ``tool_use`` blocks whose id is wanted — nothing else from this window is
    collected, so an outcome is never recorded from a result the main pass did
    not see.
    """
    start = _line_start_at_or_before(path, max(0, size - 2 * _MAX_SCAN_BYTES))
    if start is None:
        return  # no record boundary within the lookback — nothing safe to read
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            if start:
                fh.seek(start)
            read = 0
            for line in fh:
                # Budget checked BEFORE this line is counted, never after: the
                # record being recovered is by definition an oversized one, and
                # a check that fires on its own length would skip exactly it.
                if read > _MAX_SCAN_BYTES:
                    break  # into the main pass's window; it already had these
                read += len(line)
                if not wanted:
                    break
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line.strip())
                except Exception:
                    continue
                if not isinstance(rec, dict) or rec.get("isSidechain") is True:
                    continue
                msg = rec.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    tid = item.get("id")
                    if tid not in wanted or item.get("name") not in ("Edit", "Write"):
                        continue
                    finp = item.get("input")
                    fp = finp.get("file_path") if isinstance(finp, dict) else None
                    edits[tid] = (item["name"], fp if isinstance(fp, str) else None)
                    wanted.discard(tid)
    except OSError:
        return


def _scan_transcript_outcomes(data: dict) -> None:
    """Scan the session transcript and record every Edit/Write outcome.

    Main-session only (isSidechain skipped). Idempotent via UNIQUE(tool_use_id) +
    INSERT OR IGNORE, so per-turn re-scans are no-ops.
    """
    tp = data.get("transcript_path") or ""
    if not tp:
        return
    path = Path(tp).expanduser()
    try:
        if not path.exists() or not _DB_PATH.exists():
            return
    except OSError:
        return

    edits: dict[str, tuple[str, str | None]] = {}          # tool_use_id -> (tool_name, file_path)
    results: list[tuple[str, bool, dict, dict]] = []        # (tid, failed, item, rec)
    try:
        size = path.stat().st_size
        truncated = size > _MAX_SCAN_BYTES
        with path.open(encoding="utf-8", errors="replace") as fh:
            if truncated:
                # Seek one byte EARLIER than the window start, so `readline()`
                # discards the remainder of the line that straddles the cutoff —
                # and nothing else. Seeking exactly to the window start and
                # reading a line throws away a COMPLETE record whenever the
                # offset happens to land on a line boundary; from one byte back
                # that byte is the previous line's "\n" and `readline()` consumes
                # only it (Codex P2, PR #1616).
                fh.seek(size - _MAX_SCAN_BYTES - 1)
                fh.readline()
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue  # malformed/partial line → skip
                if not isinstance(rec, dict) or rec.get("isSidechain") is True:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype == "tool_use" and item.get("name") in ("Edit", "Write"):
                        tid = item.get("id")
                        if isinstance(tid, str) and tid:
                            finp = item.get("input")
                            fp = finp.get("file_path") if isinstance(finp, dict) else None
                            edits[tid] = (item["name"], fp if isinstance(fp, str) else None)
                    elif itype == "tool_result":
                        tid = item.get("tool_use_id")
                        if isinstance(tid, str) and tid:
                            results.append((tid, _result_failed(item, rec), item, rec))
    except OSError:
        return

    # A tool_use RECORD can be larger than the whole window (a big Write payload),
    # in which case the seek lands inside it, the line is discarded as partial,
    # and its tool_result — which IS in the window — has nothing to pair with. On
    # the old code that call was then skipped on this scan AND on every later one,
    # because the cutoff only ever moves forward: the outcome was lost for good.
    #
    # One extra window backwards, and only for the ids that are actually
    # unpaired, so the common case still reads exactly one window. Bounded at
    # 2 x _MAX_SCAN_BYTES; an id whose tool_use is further back than that is
    # still lost, and that is stated rather than hidden.
    unpaired = {tid for tid, _f, _i, _r in results if tid not in edits}
    if unpaired and truncated:
        _recover_earlier_tool_uses(path, size, unpaired, edits)

    rows = []
    for tid, is_err, item, rec in results:
        if tid not in edits:
            continue  # not an Edit/Write call (or its tool_use was excluded/sidechain)
        tool_name, file_path = edits[tid]
        ts = rec.get("timestamp")
        rows.append(
            (
                rec.get("session_id") or None,
                tool_name,
                file_path,
                0 if is_err else 1,
                _error_text(item, rec)[:200] if is_err else None,
                ts if isinstance(ts, str) else _now_iso(),
                tid,
            )
        )
    if rows:
        _insert_rows(rows)


def _insert_rows(rows: list[tuple]) -> None:
    """INSERT OR IGNORE outcome rows; OR IGNORE dedups on the unique tool_use_id."""
    if not rows or not _DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=2)
        try:
            conn.executemany(
                """INSERT OR IGNORE INTO tool_call_outcomes
                   (session_id, tool_name, file_path, success, error_snippet,
                    timestamp, tool_use_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        pass


if __name__ == "__main__":
    main()
