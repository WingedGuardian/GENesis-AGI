"""Name a slow SQLite statement in the log the first time it happens.

A slow statement on the server shows up only as its effect: a recall that ran
past its budget, a temp-file spill the disk guardian paged on. ``timed`` wraps
one statement and, when it took at least ``GENESIS_SQLITE_SLOW_MS`` (default
1000; ``0`` turns it off), logs one WARNING naming it:

    sqlite slow: <label> total=…ms waited=…ms ran=…ms outcome=… blocked_by=…

- ``waited`` is the time spent queueing for the connection inside this process
  (the shared write lock, or a read-pool checkout); ``ran`` is everything after
  it was obtained. ``ran`` therefore also includes SQLite's own busy-timeout
  wait for another PROCESS's lock, lock-retry backoff, and time spent behind a
  statement still running on the same worker thread.
- ``blocked_by`` names what this statement was most likely stuck behind: a
  cursor fetch in progress, else the lock holder when it started waiting, else
  a statement cancelled while holding the lock (its SQL keeps running on the
  worker thread after the caller gave up). It is best-effort: ``-`` means none
  was seen, not that there was none.
- ``outcome`` is ``ok``, ``cancelled`` (the caller gave up, e.g. the recall
  route's budget), or ``error:<ExceptionName>``.
- The label is the SQL text, whitespace-collapsed, with 32-hex names (savepoint
  ids) collapsed, and cut to 120 characters; or a read helper's qualified name.
  Parameters are NEVER logged: they carry prompts and memory content.
- Rate limit, per label and outcome: the first slow line is logged, then at
  most one per 60 s, carrying how many were suppressed; a repeat at least twice
  as slow as the last line logged is never suppressed.

Coverage: every ``SerializedConnection`` (including its cursors' row fetches)
and the recall read pool (``HybridRetriever._ro_read``), in any process that
uses them. Not timed: raw ``aiosqlite``/``sqlite3`` connections, and the two
unlocked routes ``SerializedConnection.cursor()`` and ``cursor.execute()``
(neither is used by production code). A cursor read in an ``async for`` loop is
timed per fetched batch (64 rows), so a long scan of many batches can stay
under the threshold.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from genesis.env import sqlite_slow_ms

logger = logging.getLogger(__name__)

_LOG_INTERVAL_S = 60.0
_MAX_KEYS = 1000  # bound the rate-limit table if labels ever embed literal values
_LABEL_CHARS = 120
_HEX_NAME = re.compile(r"[0-9a-f]{32}")

_clock = time.monotonic  # test seam
# (label, outcome) -> (when last logged, its total ms)
_last: dict[tuple[str, str], tuple[float, float]] = {}
_suppressed: dict[tuple[str, str], int] = {}


def sql_label(sql: object) -> str:
    """A log-safe label for one SQL statement: collapsed, cut, no parameters."""
    return _HEX_NAME.sub("<id>", " ".join(str(sql).split()))[:_LABEL_CHARS]


class timed:  # noqa: N801 - used as a context manager, reads like a function
    """Time one statement; log it when slow. Use as ``with timed(sql) as t:``
    and call ``t.acquired()`` once the connection is obtained.

    ``what`` and ``blocked_by`` are the raw SQL (or a helper label); they are
    turned into log labels only when the statement turns out slow, so a fast
    statement pays for two clock reads and one environment lookup."""

    __slots__ = ("what", "blocked_by", "_start", "_acquired_at")

    def __init__(self, what: object, *, blocked_by: object = None) -> None:
        self.what = what
        self.blocked_by = blocked_by
        self._start = 0.0
        self._acquired_at: float | None = None

    def __enter__(self) -> timed:
        self._start = _clock()
        return self

    def acquired(self) -> None:
        self._acquired_at = _clock()

    def __exit__(self, exc_type, exc, tb) -> bool:
        threshold_ms = sqlite_slow_ms()
        if threshold_ms <= 0:
            return False
        end = _clock()
        total_ms = (end - self._start) * 1000
        if total_ms < threshold_ms:
            return False
        got = self._acquired_at
        waited_ms = ((got if got is not None else end) - self._start) * 1000
        ran_ms = (end - got) * 1000 if got is not None else 0.0
        if exc_type is None:
            outcome = "ok"
        elif issubclass(exc_type, asyncio.CancelledError):
            outcome = "cancelled"
        else:
            outcome = f"error:{exc_type.__name__}"
        holder = sql_label(self.blocked_by) if self.blocked_by is not None else None
        _emit(sql_label(self.what), total_ms, waited_ms, ran_ms, outcome, holder)
        return False  # never swallow the statement's exception


def _emit(label, total_ms, waited_ms, ran_ms, outcome, blocked_by) -> None:
    now = _clock()
    key = (label, outcome)
    prev = _last.get(key)
    if prev is not None:
        last_at, last_total = prev
        if now - last_at < _LOG_INTERVAL_S and total_ms < 2 * last_total:
            _suppressed[key] = _suppressed.get(key, 0) + 1
            return
    elif len(_last) >= _MAX_KEYS:
        _last.clear()
        _suppressed.clear()
    _last[key] = (now, total_ms)
    skipped = _suppressed.pop(key, 0)
    logger.warning(
        "sqlite slow: %s total=%.0fms waited=%.0fms ran=%.0fms outcome=%s blocked_by=%s%s",
        label,
        total_ms,
        waited_ms,
        ran_ms,
        outcome,
        blocked_by or "-",
        f" (+{skipped} suppressed)" if skipped else "",
    )


def _reset() -> None:
    """Clear the rate-limit table (tests)."""
    _last.clear()
    _suppressed.clear()
