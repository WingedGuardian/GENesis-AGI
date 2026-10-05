"""What would stopping this server cancel right now?

A restart of genesis-server cancels the work it is doing: the direct-session
runner's tasks explicitly (``DirectSessionRunner.shutdown``), everything else
when the process exits. ``scripts/deploy_code_only.sh`` refuses a restart while
such work runs, and asks the server (``GET /api/genesis/inflight``) rather than
guessing from outside: a Claude process exists only for part of a session's
life, and a session row is marked complete before the result is delivered, so
both proxies miss a window the server itself knows about.

Registration is by context manager around a UNIT OF WORK:

* ``CCInvoker.run`` / ``run_streaming`` register every Claude invocation, so
  every subsystem that launches Claude is covered without its own code.
* A caller whose unit outlives the invocation (it stores, audits or delivers
  afterwards) wraps the whole unit instead; an invocation inside an open unit,
  IN THE SAME TASK, does not register separately, so one piece of work is one
  item. A task created inside a unit copies its context but can outlive it, so
  it always registers its own work: absorbing it would let it vanish from the
  report the moment its parent ends.

What it does NOT cover: a subsystem that does its own work after the
invocation returns without wrapping it (a chat turn saving and delivering its
reply, inbox or mail post-processing) is reported only while its Claude call
runs. That tail belongs at the server's shutdown, not in more registrations
(#2917).

Process-local and in memory on purpose: it describes THIS process, and a
restart that empties it is exactly the event it reports on.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = ["InflightItem", "close_unit", "inflight", "open_unit", "snapshot", "within"]


@dataclass(frozen=True)
class InflightItem:
    id: str
    kind: str
    label: str
    started_at: float  # unix seconds


_lock = threading.Lock()
_items: dict[str, InflightItem] = {}
_counter = itertools.count(1)
# Generated ids carry a tag for this process's life: the counter restarts with
# the server, so without it an id an operator copied from one refusal could name
# a different invocation after an intervening restart.
_BOOT = os.urandom(3).hex()
# The unit the current task is inside, if any, and the task that opened it: an
# invocation within it, in that same task, is part of that unit, not a second
# item.
_enclosing: ContextVar[tuple[str, object] | None] = ContextVar(
    "genesis_inflight_enclosing", default=None
)


def _task() -> object:
    """The running asyncio task, or None outside one (a thread, sync code)."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@contextmanager
def inflight(kind: str, label: str = "", item_id: str | None = None) -> Iterator[str]:
    """Register a unit of work for its whole duration.

    *item_id* names it (a session id, say) so an operator can name it back; one is
    generated otherwise. Inside a unit still open IN THIS TASK this is a no-op
    that yields the enclosing id.
    """
    outer = _enclosing.get()
    here = _task()
    if outer is not None:
        outer_id, outer_task = outer
        with _lock:
            live = outer_id in _items
        # A task created inside a unit copies its context, so it can still see
        # the unit's id, and can run on after the unit ends: only a LIVE unit
        # opened by THIS task absorbs the work.
        if live and outer_task is here:
            yield outer_id
            return
    iid = open_unit(kind, label, item_id)
    token = _enclosing.set((iid, here))
    try:
        yield iid
    finally:
        _enclosing.reset(token)
        close_unit(iid)


def open_unit(kind: str, label: str = "", item_id: str | None = None) -> str:
    """Register a unit NOW and return its id, for work whose life does not fit one
    ``with`` block: a dispatched session is registered when it is accepted, before
    its task first runs, so there is no moment where the runner holds it and the
    report does not. Pair with ``close_unit`` (a task's done-callback, say) and run
    the work ``within`` it."""
    with _lock:
        iid = item_id or f"{kind}-{_BOOT}-{next(_counter)}"
        if iid in _items:
            # A caller reused an id: keep both visible rather than hide one.
            iid = f"{iid}-{next(_counter)}"
        _items[iid] = InflightItem(id=iid, kind=kind, label=label, started_at=time.time())
    return iid


def close_unit(iid: str) -> None:
    """End a unit opened with ``open_unit``. Closing an unknown id is a no-op."""
    with _lock:
        _items.pop(iid, None)


@contextmanager
def within(iid: str) -> Iterator[str]:
    """Run the current task's work as part of an already-open unit (``open_unit``):
    work inside it, in this task, is absorbed rather than registered again."""
    token = _enclosing.set((iid, _task()))
    try:
        yield iid
    finally:
        _enclosing.reset(token)


def snapshot() -> list[InflightItem]:
    """Every registered unit, oldest first."""
    with _lock:
        items = list(_items.values())
    return sorted(items, key=lambda i: i.started_at)
