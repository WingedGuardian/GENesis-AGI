"""Sent-alert log: a record of what the Guardian actually posted to Telegram.

Genesis records every outbound Telegram message it sends itself
(``telegram_messages``), but the Guardian posts through its own stdlib client on
the host, so its alerts never reach that table and never appear in conversation
history. This log closes the gap: each successfully delivered ALERT is appended
here, and the container pulls new entries through the read-only ``sent-log``
gateway verb and stores them with ``sender="guardian"``.

Scope (owner ruling): alerts only. Approval prompts (``send_text``) are not
recorded, and a send that returned no ``message_id`` is not recorded.

Bounding mirrors ``alert/queue.py``: an age bound (``MAX_AGE_S``, the queue's
14 days, long enough to cover a Genesis outage the container later catches up
on) plus a size bound. The size bound is two JSONL generations: when the live
file would exceed ``MAX_BYTES`` it is renamed to ``<name>.1`` (replacing the
older generation) and a fresh file starts, so the log never exceeds roughly
``2 * MAX_BYTES`` on disk.

Stdlib only, no other ``genesis`` imports: it runs in the host venv (stdlib +
pyyaml, see tests/test_guardian/test_host_import_contract.py). Writing is
best-effort and never raises, so a full disk or a permissions problem can never
stop an alert from being sent or the tick from finishing.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_BYTES = 512 * 1024
MAX_AGE_S = 14 * 24 * 3600  # same horizon as alert/queue.py _PRUNE_MAX_AGE_S
# Entries returned per read. The container pages with its cursor, so this only
# bounds one SSH response (a Telegram message is at most 4,096 UTF-16 units).
READ_LIMIT = 100
KINDS = frozenset({"alert"})
_INT64_MAX = 2**63 - 1  # SQLite INTEGER range, where the container stores ids
# Telegram caps a message at 4,096 UTF-16 units of PARSED text; this is the raw
# HTML, whose escapes parse back to one character each, so it can run longer.
# 8x the parsed cap: no alert the formatter builds comes near it.
MAX_TEXT_CHARS = 32_768
# The critical section is a stat, a rename and one write: milliseconds. Five
# seconds is far past any healthy holder and still short for a tick.
_LOCK_WAIT_S = 5.0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def entry_problem(entry: Any) -> str | None:
    """Name the field that makes ``entry`` unusable, or None. Never raises.

    Kept in step with the container's reader (guardian/sent_log_ingest.py
    ``_valid_entry``): an entry the container would reject must never be
    written, or a page of them would hold its cursor in place until it ages out.
    """
    if not isinstance(entry, dict):
        return "entry"
    ts = entry.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return "ts"
    try:
        if not math.isfinite(ts) or not 0 <= ts < 1e10:
            return "ts"
    except OverflowError:
        return "ts"
    chat_id = entry.get("chat_id")
    if not _is_int(chat_id) or chat_id == 0 or abs(chat_id) > _INT64_MAX:
        return "chat_id"
    thread_id = entry.get("thread_id")
    if thread_id is not None and (not _is_int(thread_id) or not 0 < thread_id <= _INT64_MAX):
        return "thread_id"
    message_id = entry.get("message_id")
    if not _is_int(message_id) or not 0 < message_id <= _INT64_MAX:
        return "message_id"
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        return "text"
    if entry.get("kind") not in KINDS:
        return "kind"
    return None


def _lock(fd: int) -> bool:
    """Take the exclusive lock, waiting at most ``_LOCK_WAIT_S``. A holder that
    is stuck (a stopped --test run, say) must not stall the tick: the alert is
    already delivered, so giving up only costs its record."""
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def append_sent(
    path: Path | str,
    *,
    chat_id: int,
    thread_id: int | None,
    message_id: int,
    text: str,
    kind: str = "alert",
) -> bool:
    """Append one delivered message. Returns True when written. Never raises."""
    try:
        entry = {
            "ts": time.time(),
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "text": text,
            "kind": kind,
        }
        problem = entry_problem(entry)
        if problem is not None:
            logger.warning("sent-log entry not written: bad %s", problem)
            return False
        path = Path(path).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(
            path.with_name(path.name + ".lock"),
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600,
        )
        try:
            # Serialise rotate-then-append across processes (the tick and a
            # concurrent --test run), so a rotation never splits a write.
            if not _lock(lock_fd):
                logger.warning("sent-log lock busy for %ss; alert not recorded", _LOCK_WAIT_S)
                return False
            # Stamp UNDER the lock, so file order is ts order: the container's
            # cursor is "ts >= newest stored", and an entry written after a
            # later-stamped one could otherwise fall behind it and never be read.
            entry["ts"] = time.time()
            line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_size + len(line) > MAX_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                # A torn last line (a short write on a full disk) would swallow
                # this entry into it: start a fresh line first.
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    line = b"\n" + line
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
        return True
    except Exception:
        logger.warning("sent-log append failed; alert was still sent", exc_info=True)
        return False


def read_since(
    path: Path | str,
    since: float,
    *,
    limit: int = READ_LIMIT,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Return valid entries with ``ts >= since``, oldest first, at most ``limit``.

    The bound is inclusive so an entry sharing the cursor's timestamp is never
    skipped; the container's UNIQUE(chat_id, message_id, direction) makes the
    repeat harmless. Entries past ``MAX_AGE_S`` and malformed lines are dropped.
    The live file is read before the older generation, so a rotation between the
    two reads can only duplicate entries, never lose them.
    """
    path = Path(path).expanduser()
    floor = max(since, (time.time() if now is None else now) - MAX_AGE_S)
    seen: set[tuple[int, int]] = set()
    out: list[dict[str, Any]] = []
    for candidate in (path, path.with_name(path.name + ".1")):
        try:
            raw = candidate.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("sent-log %s unreadable", candidate.name, exc_info=True)
            continue
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if entry_problem(entry) is not None or entry["ts"] < floor:
                continue
            key = (entry["chat_id"], entry["message_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    out.sort(key=lambda e: e["ts"])
    return out[:limit]
