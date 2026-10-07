"""Graph-traversal telemetry: the FalkorDB default-on cutover's durable counter.

``memory/graph.py``'s ``traverse`` records every traversal outcome here. Kept
out of the facade so that module stays about choosing and calling a store.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)


# ── Traversal telemetry: the FalkorDB cutover's durable counter ─────────────
#
# The default-on cutover waits on 14 days of falkordb mode with zero fallbacks.
# Logs cannot answer that: the fallback warnings in ``memory/graph.py`` come
# mostly from MCP servers, which log to stderr that never reaches the journal.
# So every traversal's outcome lands in ``eval_events``: ONE row per caller call
# (a "tally"), plus a row of its own, written the moment it happens, for the
# FIRST clock-breaking outcome of the call. A process killed mid-request can
# therefore never lose the evidence that the clock broke; any later fallbacks in
# the same call ride on the closing row. Only the first is written at once
# because the recall graph budget counts traversal time, not this write, and an
# engine outage makes every traversal a fallback. Clock-breaking outcomes carry
# an event entry with the exception class names (never messages, ids or paths).
#
# A row that fails to write is counted twice over: in memory, carried on the
# next row this process writes (``prior_write_failures``), and as one line in
# a local file (``lost_writes_path()``) that survives the process. The verdict
# reads either as INCONCLUSIVE. Only a failure of the database AND the file at
# the same moment goes uncounted. Callers close their tally in ``finally``, so
# an error or cancellation inside the call still writes what it collected.

#: ``eval_events.event_type`` for every traversal-telemetry row.
TELEMETRY_EVENT_TYPE = "graph_traverse"
#: Pruned after this many days by the learning scheduler's
#: ``graph_traverse_prune``; comfortably longer than the 14-day verdict window.
TELEMETRY_RETENTION_DAYS = 30
#: Operator kill switch: ``1`` records nothing.
_TELEMETRY_OFF_ENV = "GENESIS_GRAPH_TELEMETRY_DISABLED"
#: Outcomes that break the cutover clock: written at once, with an event entry.
_CLOCK_BREAKING = frozenset({"fallback", "selection_failed", "error"})
#: One JSON line per telemetry row that failed to write, under ``genesis_home()``.
LOST_WRITES_FILE = "telemetry/graph_traverse_lost_writes.jsonl"
#: Interpreter options that consume the next argument (``python -X dev script``).
_PY_OPTS_WITH_VALUE = frozenset({"-X", "-W", "-Q"})

#: Telemetry rows this process failed to write since the last one that landed.
_write_failures = 0
_proc_role: str | None = None


@dataclass
class _Tally:
    caller: str
    #: Where an immediate row is written when the caller's ``db`` is read-only.
    db_path: str | None = None
    #: The call's first clock-breaking outcome has already been written alone.
    wrote_breaking: bool = False
    traversals: int = 0
    outcomes: Counter = field(default_factory=Counter)
    served: Counter = field(default_factory=Counter)
    configured: Counter = field(default_factory=Counter)
    events: list[dict] = field(default_factory=list)

    def add(
        self,
        *,
        outcome: str,
        configured: str,
        served_by: str,
        primary_reason: str | None,
        final_reason: str | None,
    ) -> None:
        self.traversals += 1
        self.outcomes[outcome] += 1
        self.served[served_by] += 1
        self.configured[configured] += 1
        if outcome in _CLOCK_BREAKING:
            self.events.append(
                {
                    "outcome": outcome,
                    "configured": configured,
                    "served_by": served_by,
                    "primary_reason": primary_reason,
                    "final_reason": final_reason,
                }
            )


#: The tally open for the current call, shared by every traversal it makes.
_TALLY: contextvars.ContextVar[_Tally | None] = contextvars.ContextVar(
    "graph_traversal_tally",
    default=None,
)
#: ``(effective_mode, selection_error_class)`` published by ``_traversal_store``
#: for the ``traverse`` call that just asked it. ``traverse`` resets it first, so
#: a stale value from an earlier call can never be read.
_SELECTION: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar(
    "graph_store_selection",
    default=None,
)


def reset_selection() -> None:
    """Clear the published selection before ``_traversal_store`` runs, so a
    value left by an earlier traversal can never be read for this one."""
    _SELECTION.set(None)


def publish_selection(mode: str, error: str | None) -> None:
    _SELECTION.set((mode, error))


def read_selection() -> tuple[str, str | None]:
    """``(effective_mode, selection_error_class)`` for the current traversal;
    ``("unknown", None)`` when the store was chosen without the lever (tests)."""
    return _SELECTION.get() or ("unknown", None)


def _telemetry_off() -> bool:
    return os.environ.get(_TELEMETRY_OFF_ENV) == "1"


def exception_reason(exc: BaseException) -> str:
    """The class name of what actually failed. Store errors arrive wrapped in
    ``GraphUnavailableError``, so the chained cause names the real failure
    (timeout, refused socket, missing projection)."""
    cause = exc.__cause__
    return type(cause if cause is not None else exc).__name__


def mcp_server_name(argv: list[str]) -> str | None:
    """The ``--server`` value of a ``genesis_mcp_server.py`` process, ``""`` when
    it is one but the value cannot be read, ``None`` when it is not one.

    Only a python interpreter running the script counts (the script is the first
    argument that is not an interpreter option), so an editor, ``git`` or a
    shell string that merely names the file is not an MCP server. Accepts both
    ``--server memory`` and ``--server=memory``, as argparse does."""
    if not argv or not os.path.basename(argv[0]).startswith("python"):
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _PY_OPTS_WITH_VALUE else 1
    if i >= len(argv) or os.path.basename(argv[i]) != "genesis_mcp_server.py":
        return None
    rest = argv[i + 1 :]
    for j, arg in enumerate(rest):
        if arg.startswith("--server="):
            return arg.split("=", 1)[1]
        if arg == "--server" and j + 1 < len(rest):
            return rest[j + 1]
    return ""


def process_role(argv: list[str] | None = None) -> str:
    """Which kind of process is traversing, from its original command line."""
    if argv is None:
        argv = list(getattr(sys, "orig_argv", None) or sys.argv)
    server = mcp_server_name(argv)
    if server is not None:
        return f"mcp-{server or 'unknown'}"
    names = [os.path.basename(a) for a in argv]
    if "ambient_awareness_worker.py" in names:
        return "ambient"
    if "-m" in argv and argv.index("-m") + 1 < len(argv):
        i = argv.index("-m")
        module, after = argv[i + 1], argv[i + 2 :]
        if module == "genesis" and after[:1] == ["serve"]:
            return "server"
        if module == "genesis.channels.bridge":
            return "bridge"
    return "other"


def begin_tally(caller: str, *, db_path: str | None = None) -> contextvars.Token | None:
    """Open a tally for one caller call. Returns ``None`` when one is already
    open, in which case this call's traversals join it and ``end_tally`` with
    that ``None`` does nothing. ``db_path`` is where rows go when the caller
    traverses on a read-only connection."""
    if _TALLY.get() is not None:
        return None
    return _TALLY.set(_Tally(caller=caller, db_path=db_path))


async def end_tally(
    token: contextvars.Token | None,
    db: aiosqlite.Connection | None,
    *,
    db_path: str | None = None,
) -> None:
    """Close the tally ``begin_tally`` opened and write its row. ``db_path``
    writes through a short-lived read-write connection instead of ``db`` (for a
    caller that traverses on a read-only one). Never raises an ``Exception``; a
    cancellation during the write is counted as a lost row and re-raised."""
    if token is None:
        return
    tally = _TALLY.get()
    _TALLY.reset(token)
    if tally is not None and tally.traversals:
        await _write_tally(tally, db, db_path=db_path if db_path is not None else tally.db_path)


@asynccontextmanager
async def traversal_tally(
    db: aiosqlite.Connection | None,
    *,
    caller: str,
    db_path: str | None = None,
) -> AsyncIterator[None]:
    """Record every traversal made inside the block as ONE telemetry row."""
    token = begin_tally(caller, db_path=db_path)
    try:
        yield
    finally:
        await end_tally(token, db, db_path=db_path)


async def note_traversal(db: aiosqlite.Connection, **outcome: str | None) -> None:
    """Record one traversal. The open tally takes it, except the call's FIRST
    clock-breaking outcome, which is written as its own row now so a process
    killed before the tally closes cannot take the broken clock with it. With
    no tally open, every traversal is its own row."""
    if _telemetry_off():
        return
    tally = _TALLY.get()
    breaking = outcome.get("outcome") in _CLOCK_BREAKING
    if tally is not None and (not breaking or tally.wrote_breaking):
        tally.add(**outcome)
        return
    if tally is not None:
        tally.wrote_breaking = True
    row = _Tally(caller=tally.caller if tally is not None else "direct")
    row.add(**outcome)
    await _write_tally(row, db, db_path=tally.db_path if tally is not None else None)


async def _write_tally(
    tally: _Tally,
    db: aiosqlite.Connection | None,
    *,
    db_path: str | None = None,
) -> None:
    global _write_failures, _proc_role
    if _telemetry_off():
        return
    if _proc_role is None:
        _proc_role = process_role()
    carried = _write_failures
    metrics = {
        "caller": tally.caller,
        "proc": _proc_role,
        "traversals": tally.traversals,
        "outcomes": dict(tally.outcomes),
        "served": dict(tally.served),
        "configured": dict(tally.configured),
        "events": tally.events,
        "prior_write_failures": carried,
    }
    try:
        from genesis.db.crud import j9_eval

        if db_path is not None:
            from genesis.db.connection import get_raw_db

            async with get_raw_db(db_path) as conn:
                await j9_eval.insert_event(
                    conn,
                    dimension="system",
                    event_type=TELEMETRY_EVENT_TYPE,
                    metrics=metrics,
                )
        elif db is not None:
            await j9_eval.insert_event(
                db,
                dimension="system",
                event_type=TELEMETRY_EVENT_TYPE,
                metrics=metrics,
            )
        else:
            raise RuntimeError("no connection to write the telemetry row on")
    except BaseException as exc:
        # Counted for ANY failure, cancellation included: a row that may carry a
        # fallback and did not land must never read as a clean day.
        _write_failures += 1
        _record_lost_write(tally, exc)
        logger.warning(
            "graph traversal telemetry row not written (caller=%s, %d traversal(s)); "
            "the FalkorDB cutover verdict reads this as INCONCLUSIVE",
            tally.caller,
            tally.traversals,
            exc_info=True,
        )
        if not isinstance(exc, Exception):
            raise
        return
    # Only what this row carried: failures counted while it was being written
    # stay for the next row.
    _write_failures = max(0, _write_failures - carried)


def lost_writes_path() -> Path:
    """The local file that outlives a failed telemetry write (see module comment)."""
    from genesis.env import genesis_home

    return genesis_home() / LOST_WRITES_FILE


def prune_lost_writes(days: int = TELEMETRY_RETENTION_DAYS) -> int:
    """Drop lost-write lines older than ``days``; returns how many were dropped.
    An unreadable line is kept (it may be the only trace of a lost row)."""
    from datetime import timedelta

    path = lost_writes_path()
    if not path.exists():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    keep: list[str] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            old = json.loads(line)["ts"] < cutoff
        except (ValueError, KeyError, TypeError):
            old = False
        if old:
            dropped += 1
        else:
            keep.append(line)
    if dropped:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(f"{line}\n" for line in keep), encoding="utf-8")
        tmp.replace(path)
    return dropped


def _record_lost_write(tally: _Tally, exc: BaseException) -> None:
    """Append one line for a row that did not land. Synchronous on purpose: it
    runs inside the failure handler, where an ``await`` could itself be
    cancelled. Never raises."""
    try:
        path = lost_writes_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "caller": tally.caller,
                "proc": _proc_role,
                "traversals": tally.traversals,
                "clock_breaking": len(tally.events),
                "error": type(exc).__name__,
            }
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.warning("graph traversal lost-write record also failed", exc_info=True)
