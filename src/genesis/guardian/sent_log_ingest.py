"""Ingest the Guardian's sent-alert log into telegram_messages — CONTAINER-SIDE.

The host Guardian posts alerts to Telegram through its own client, so they never
pass through the channel adapter that records Genesis's outbound messages, and
conversation history cannot see them. The Guardian appends each delivered alert
to a bounded log (``guardian/alert/sent_log.py``, host side); this module pulls
new entries through the read-only ``sent-log --since <epoch>`` gateway verb and
stores each with ``sender="guardian"``, ``direction="outbound"``.

Cursor: no new store. The cursor is derived as MAX(timestamp) of the guardian
rows already stored. The host bound is inclusive and UNIQUE(chat_id, message_id,
direction) makes re-reading an entry a no-op, so a repeat is harmless and no
entry at the cursor's timestamp is skipped. First run (no guardian rows) asks
from 0; the host's own 14-day age bound and per-call page limit bound that read.

Tolerant of an old or broken Guardian: an unknown verb, a skew error, a timeout
or a malformed entry is a debug-level no-op. A reachable Guardian whose response
lacks the terminal ok line (a drifted protocol, which would otherwise look like
"no alerts") logs one WARNING per process, then debug. Nothing here sends an
alert, and nothing raises into the awareness tick.

The log's text reaches conversation history, so every entry is validated as
untrusted data: exact types, a closed ``kind`` set, positive ids inside SQLite's
integer range, and a size ceiling on the text (rejected, never cut).

Kill switch: ``GENESIS_GUARDIAN_SENT_LOG_DISABLED=1`` skips the pull entirely.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["ingest_guardian_sent_log"]

_DISABLE_ENV = "GENESIS_GUARDIAN_SENT_LOG_DISABLED"
_KINDS = frozenset({"alert"})
_INT64_MAX = 2**63 - 1
# A Telegram message is at most 4,096 UTF-16 units of PARSED text; the stored
# copy is the HTML the Guardian sent, whose escapes (&amp; &lt; ...) parse back to
# one character each, so the raw form can be several times longer. 32,768 is
# 8x the parsed cap: no real alert reaches it, and anything that does is not an
# alert we sent.
_MAX_TEXT_CHARS = 32_768
# The host returns at most 100 entries per call (sent_log.READ_LIMIT). A
# response with more lines than this, or larger than this many bytes, is not
# one the host produces, and is refused whole.
_MAX_LINES = 200
_MAX_OUTPUT_CHARS = _MAX_LINES * (_MAX_TEXT_CHARS + 512)
# A row stamped in the future would sort as the newest message of its chat in
# every recap for as long as the clock lags it. The Guardian shares the host
# clock, so real skew is ~0; five minutes allows for an NTP step.
_MAX_FUTURE_S = 300.0
# One WARNING per process when a reachable Guardian answers off-protocol.
_drift_warned = False


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_entry(entry: Any) -> dict[str, Any] | None:
    """Return the fields to store, or None when the entry is unusable."""
    if not isinstance(entry, dict):
        return None
    ts = entry.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        if not math.isfinite(ts) or not 0 <= ts <= time.time() + _MAX_FUTURE_S:
            return None
    except OverflowError:
        return None
    chat_id = entry.get("chat_id")
    if not _is_int(chat_id) or not -_INT64_MAX <= chat_id <= _INT64_MAX or chat_id == 0:
        return None
    thread_id = entry.get("thread_id")
    if thread_id is not None and (not _is_int(thread_id) or not 0 < thread_id <= _INT64_MAX):
        return None
    message_id = entry.get("message_id")
    if not _is_int(message_id) or not 0 < message_id <= _INT64_MAX:
        return None
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS:
        return None
    try:
        text.encode("utf-8")  # a lone surrogate would fail at the SQLite bind
    except UnicodeEncodeError:
        return None
    if entry.get("kind") not in _KINDS:
        return None
    return {
        "chat_id": chat_id,
        "thread_id": thread_id,
        "message_id": message_id,
        "content": text,
        "timestamp": datetime.fromtimestamp(ts, UTC).isoformat(timespec="microseconds"),
    }


def _parse_response(output: str) -> list[Any] | None:
    """Split the verb's output into entry objects, or None when it is not a
    complete response (no terminal ok line, a count mismatch, oversized)."""
    if len(output) > _MAX_OUTPUT_CHARS:
        return None
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if not lines or len(lines) > _MAX_LINES:
        return None
    try:
        terminal = json.loads(lines[-1])
    except (ValueError, RecursionError):
        return None
    if not (
        isinstance(terminal, dict)
        and terminal.get("ok") is True
        and terminal.get("action") == "sent-log"
        and _is_int(terminal.get("count"))
        and terminal["count"] == len(lines) - 1
    ):
        return None
    entries: list[Any] = []
    for line in lines[:-1]:
        try:
            entries.append(json.loads(line))
        except (ValueError, RecursionError):
            entries.append(None)
    return entries


async def _cursor(db) -> float:
    """Epoch seconds of the newest guardian row already stored; 0.0 if none.

    Rows stamped after NOW are left out: one entry from a clock that ran ahead
    (or a clock later stepped back) would otherwise pin the cursor in the future
    and every later alert, with a smaller ts, would never be pulled. Excluding it
    costs one deduplicated re-read per tick until real time passes it.
    """
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    cursor = await db.execute(
        "SELECT MAX(timestamp) FROM telegram_messages "
        "WHERE sender = 'guardian' AND direction = 'outbound' AND timestamp <= ?",
        (now,),
    )
    row = await cursor.fetchone()
    if not row or not row[0]:
        return 0.0
    try:
        parsed = datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, parsed.timestamp())


async def _already_stored(db, fields: dict[str, Any]) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM telegram_messages "
        "WHERE chat_id = ? AND message_id = ? AND direction = 'outbound'",
        (fields["chat_id"], fields["message_id"]),
    )
    return await cursor.fetchone() is not None


async def ingest_guardian_sent_log(db, *, remote) -> int:
    """Pull new Guardian alerts and store them. Returns the rows newly stored.

    Never raises: the awareness tick calls this every cycle.
    """
    global _drift_warned
    if os.environ.get(_DISABLE_ENV) == "1":
        return 0
    try:
        since = await _cursor(db)
        ok, output = await remote.sent_log(since)
        if not ok:
            # Old gateway ("denied"), src skew, unreachable host, timeout.
            logger.debug("Guardian sent-log unavailable: %.200r", output)
            return 0
        entries = _parse_response(output)
        if entries is None:
            # The verb ran and succeeded but the body is not the protocol: a
            # drifted Guardian, not an old one. Say so once per process (it
            # would otherwise look exactly like "no alerts"), then stay quiet.
            if not _drift_warned:
                _drift_warned = True
                logger.warning("Guardian sent-log response is not the expected protocol; ignored")
            else:
                logger.debug("Guardian sent-log response incomplete; ignored")
            return 0

        from genesis.db.crud import telegram_messages

        stored = 0
        for raw in entries:
            fields = _valid_entry(raw)
            if fields is None:
                logger.debug("Guardian sent-log entry malformed; skipped")
                continue
            # The host bound is inclusive, so the newest row comes back every
            # tick; skip it rather than count an INSERT OR IGNORE that did nothing.
            if await _already_stored(db, fields):
                continue
            try:
                await telegram_messages.store(
                    db,
                    sender="guardian",
                    direction="outbound",
                    **fields,
                )
            except Exception:
                # One bad row must not abort the page: the rest still land,
                # and the cursor moves past it with them.
                logger.debug("Guardian sent-log entry not stored; skipped", exc_info=True)
                continue
            stored += 1
        return stored
    except Exception:
        logger.warning("Guardian sent-log ingest failed", exc_info=True)
        return 0
