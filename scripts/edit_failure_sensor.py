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


def _is_error(value: object) -> bool:
    """True iff a tool_result marks a failure. Successful results OMIT is_error
    (verified: 941/1023 real Edit/Write results had no is_error key). Accept the
    JSON bool plus 1/"true" so a payload-format drift can't silently re-blind the
    failure path (the exact class #1597 fixed); everything else is a success."""
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() == "true")


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
    results: list[tuple[str, bool, dict, dict]] = []        # (tid, is_error, item, rec)
    try:
        size = path.stat().st_size
        with path.open(encoding="utf-8", errors="replace") as fh:
            if size > _MAX_SCAN_BYTES:
                fh.seek(size - _MAX_SCAN_BYTES)
                fh.readline()  # discard the partial first line after the seek
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
                            results.append((tid, _is_error(item.get("is_error")), item, rec))
    except OSError:
        return

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
