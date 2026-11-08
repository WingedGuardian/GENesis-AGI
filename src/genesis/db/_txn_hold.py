"""Name a SQLite write transaction that stays open too long, and who opened it.

In WAL mode one connection at a time holds the database's writer slot, from its
first write in a transaction until COMMIT or ROLLBACK. While a shared
:class:`~genesis.db.connection.SerializedConnection` sits inside a transaction,
every other PROCESS that wants to write waits, and gives up with "database is
locked" after its busy_timeout (5 s here). The slow-statement log
(``_slow_log``) cannot see this: the statements are fast, it is the GAP between
the write that opened the transaction and the commit that closed it that holds
the slot. The hypothesis this instrument tests is that such gaps on the
server's shared connection are what other processes lose to.

``TxnHoldTracker`` is fed after every operation on one connection (from
``SerializedConnection._locked_timed``, and from ``close()``) with whether the
connection is now in a transaction. When a transaction opens it records when,
the statement, and the code that issued it; when it closes, if it was held at
least ``GENESIS_SQLITE_TXN_HOLD_MS`` (default 1000; ``0`` turns this off), it
logs one WARNING:

    sqlite txn held: <ms>ms opened_by=<label> at <site> via <origin> task=<name>
    closed_by=<label> at <site> loop_lag=<yes|no>

How to read it:

- ``site`` is the nearest caller outside the connection plumbing (usually a
  ``genesis.db.crud`` function); ``origin`` is the nearest caller outside
  ``genesis.db`` and its driver altogether, the code that owns the work.
  ``(cancelled)`` means the opening statement was cancelled, so its caller is
  gone from the stack.
- ``opened_by`` is the code that had the transaction OPEN, not necessarily the
  cause of the delay. A stalled event loop holds every open transaction for
  the whole stall, so with ``loop_lag=yes`` (the loop-lag sampler saw lag
  during the hold) the cause is most likely the stalled frame named in that
  window's ``loop-stall`` log line, and the opener is a bystander. ``no`` can
  also mean the sampler had not published yet; check the stall log either way.
- The clock starts when the opening statement RETURNS, so a stall that
  overlapped that statement's own run is not counted.
- Python's legacy transaction mode (the only one this repo uses) opens a
  transaction only before a write, so an open transaction normally means the
  connection holds the writer slot. A bare ``BEGIN`` or ``SAVEPOINT`` followed
  only by reads is reported too, though it may hold only a read snapshot.
- Labels are ``_slow_log.sql_label`` (no parameters ever). Rate limit, per
  (opener, origin): the first line is logged, then at most one per 60 s
  carrying how many were suppressed; a hold at least twice as long as the last
  one logged is never suppressed.

COVERAGE: ``SerializedConnection`` only, which includes the server's shared
connection. Owned ``get_raw_db`` / raw ``aiosqlite`` connections (for example
the knowledge batch's own ``BEGIN IMMEDIATE`` envelope) are NOT observed, so
the absence of a line does not clear them. A transaction opened through the
unlocked ``cursor()`` route is not observed either (no production caller).

Observability only: it never changes how a statement runs, and a failure inside
it is swallowed (a broken instrument must not break the write path).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from genesis.db._slow_log import sql_label
from genesis.env import sqlite_txn_hold_ms

logger = logging.getLogger(__name__)

_LOG_INTERVAL_S = 60.0
_MAX_KEYS = 1000
# The lag sampler publishes a stall once it CLEARS, about one interval (0.5 s)
# after it ends; widen the lookback by that much so a stall right before the
# commit is still seen.
_LAG_SLACK_S = 1.0
# Frames that are the DB plumbing or its driver, never the caller worth naming.
_SITE_SKIP = (
    "genesis.db.connection",
    "genesis.db.lock_holders",
    "genesis.db._",
    "aiosqlite",
    "contextlib",
    "asyncio",
)
_ORIGIN_SKIP = ("genesis.db", "aiosqlite", "contextlib", "asyncio")

_clock = time.monotonic  # test seam
_last: dict[tuple[str, str], tuple[float, float]] = {}
_suppressed: dict[tuple[str, str], int] = {}


def _callers() -> tuple[str, str]:
    """(nearest frame outside the connection plumbing, nearest outside genesis.db)."""
    site = origin = "?"
    try:
        frame = sys._getframe(2)
        while frame is not None:
            module = frame.f_globals.get("__name__", "?")
            if site == "?" and not module.startswith(_SITE_SKIP):
                site = f"{module}:{frame.f_code.co_name}"
            if not module.startswith(_ORIGIN_SKIP):
                origin = f"{module}:{frame.f_code.co_name}"
                break
            frame = frame.f_back
    except Exception:  # noqa: BLE001 - attribution is best-effort
        pass
    return site, origin


def _task_name() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return "-"
    return task.get_name() if task is not None else "-"


def _loop_lagged(held_s: float) -> str:
    """Whether the loop-lag sampler recorded lag during the hold (``yes``/``no``)."""
    try:
        from genesis.util import loop_health

        return "yes" if loop_health.recently_lagging(held_s + _LAG_SLACK_S) else "no"
    except Exception:  # noqa: BLE001 - attribution is best-effort
        return "?"


class TxnHoldTracker:
    """Per-connection open-transaction bookkeeping. Not thread-safe: it is fed
    only while the owning connection's asyncio lock is held."""

    __slots__ = ("opened_at", "opener", "site", "origin", "task")

    def __init__(self) -> None:
        self.opened_at: float | None = None
        self.opener = self.site = self.origin = self.task = ""

    def observe(self, in_transaction: bool | None, sql: object, *, cancelled: bool = False) -> None:
        """Record the connection's state after one operation (``sql``)."""
        try:
            if in_transaction is None:
                return  # unreadable (closed, reconnecting): change nothing
            if in_transaction and self.opened_at is None:
                if sqlite_txn_hold_ms() <= 0:
                    return
                self.opened_at = _clock()
                self.opener = sql_label(sql)
                if cancelled:
                    self.site = self.origin = "(cancelled)"
                else:
                    self.site, self.origin = _callers()
                self.task = _task_name()
            elif not in_transaction and self.opened_at is not None:
                held_ms = (_clock() - self.opened_at) * 1000
                self.opened_at = None
                threshold = sqlite_txn_hold_ms()
                if threshold > 0 and held_ms >= threshold:
                    closer_site = "(cancelled)" if cancelled else _callers()[0]
                    _emit(
                        held_ms,
                        self.opener,
                        self.site,
                        self.origin,
                        self.task,
                        sql_label(sql),
                        closer_site,
                        _loop_lagged(held_ms / 1000),
                    )
        except Exception:  # noqa: BLE001 - observability must not break writes
            logger.debug("txn-hold tracking failed", exc_info=True)


def _emit(held_ms, opener, site, origin, task, closer, closer_site, loop_lag) -> None:
    now = _clock()
    key = (opener, origin)
    prev = _last.get(key)
    if prev is not None:
        last_at, last_held = prev
        if now - last_at < _LOG_INTERVAL_S and held_ms < 2 * last_held:
            _suppressed[key] = _suppressed.get(key, 0) + 1
            return
    elif len(_last) >= _MAX_KEYS:
        _last.clear()
        _suppressed.clear()
    _last[key] = (now, held_ms)
    skipped = _suppressed.pop(key, 0)
    logger.warning(
        "sqlite txn held: %.0fms opened_by=%s at %s via %s task=%s closed_by=%s at %s "
        "loop_lag=%s%s",
        held_ms,
        opener,
        site,
        origin,
        task,
        closer,
        closer_site,
        loop_lag,
        f" (+{skipped} suppressed)" if skipped else "",
    )


def _reset() -> None:
    """Clear the rate-limit table (tests)."""
    _last.clear()
    _suppressed.clear()
