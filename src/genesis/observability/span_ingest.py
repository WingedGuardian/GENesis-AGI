"""Drain CC-tool span records from the flat-file drop dir into ``otel_spans``.

The CC PostToolUse hook (``scripts/hooks/cc_span_hook.py``) runs in the CC
subprocess and appends one JSONL line per tool call to
``~/.genesis/spans/incoming/<key>.jsonl`` — it cannot safely write the shared DB
from a high-frequency cross-process hook (WAL-lock history). This server-side
ingest runs on a periodic job, drains those files on the server's own event
loop, and batch-inserts into ``otel_spans``.

Atomic-rename-then-read: each file is renamed to ``.processing`` before reading
so the hook (which always opens the original name) starts a fresh file for new
writes — no lost/double lines beyond the sub-ms rename window, and INSERT OR
IGNORE makes any duplicate harmless. Corrupt/partial JSONL lines are skipped.
Honors ``GENESIS_SPANS_INCOMING_DIR`` (matches the hook's override).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

# Single source of column order — the same INSERT the in-process writer uses,
# so ingest rows can never drift from the schema.
from genesis.observability.span_writer import _INSERT as _SPAN_INSERT

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_MAX_LINES_PER_FILE = 5000  # safety cap against a runaway file


def _incoming_dir() -> Path:
    override = os.environ.get("GENESIS_SPANS_INCOMING_DIR")
    return Path(override or os.path.expanduser("~/.genesis/spans/incoming"))


def _incoming_files() -> list[Path]:
    d = _incoming_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.jsonl"))


def _row(rec: dict) -> tuple | None:
    """Map a hook JSONL record to the otel_spans column tuple (process=cc-hook).

    Column order matches ``span_writer._COLS``. LLM-only columns are NULL for
    tool spans. Returns None for a malformed record (missing required field).
    """
    try:
        start = int(rec["start_unix_us"])
        return (
            str(rec["span_id"]),
            str(rec["trace_id"]),
            rec.get("parent_span_id"),
            str(rec["name"]),
            str(rec.get("kind", "tool")),
            str(rec.get("status", "ok")),
            rec.get("status_message"),
            start,
            rec.get("end_unix_us"),
            rec.get("duration_us"),
            rec.get("session_id"),
            "cc-hook",
            None, None, None, None, None, None, None,  # LLM block — n/a for tools
            json.dumps(rec["attributes"]) if rec.get("attributes") else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


async def ingest_pending_spans(db: aiosqlite.Connection) -> int:
    """Drain all incoming span files into otel_spans. Returns rows inserted."""
    files = _incoming_files()
    if not files:
        return 0
    total = 0
    for f in files:
        proc = f.with_suffix(".jsonl.processing")
        try:
            os.rename(f, proc)
        except OSError:
            continue  # already taken by another pass / removed
        rows: list[tuple] = []
        try:
            with open(proc) as fh:
                for i, line in enumerate(fh):
                    if i >= _MAX_LINES_PER_FILE:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate corrupt/partial lines
                    row = _row(rec)
                    if row is not None:
                        rows.append(row)
            if rows:
                await db.executemany(_SPAN_INSERT, rows)
                await db.commit()
                total += len(rows)
        except Exception:
            logger.debug("span ingest failed for %s", proc.name, exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                proc.unlink()
    return total
