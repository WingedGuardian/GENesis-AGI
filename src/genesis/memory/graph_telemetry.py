"""Graph-traversal telemetry: the FalkorDB default-on cutover's durable counter.

``memory/graph.py``'s ``traverse`` records every traversal outcome here. Kept
out of the facade so that module stays about choosing and calling a store.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)


# ── Traversal telemetry: the FalkorDB cutover's durable counter ─────────────
#
# The default-on cutover waits on 14 days of falkordb mode with zero fallbacks.
# Logs cannot answer that: the fallback warnings in ``memory/graph.py`` come
# mostly from MCP servers, which log to stderr that never reaches the journal. So every traversal's outcome lands in
# ``eval_events`` — ONE row per caller call (a "tally"), or one row per
# traversal when no tally is open. Clock-breaking outcomes carry an event entry
# with the exception class names (never messages, ids or paths).
#
# A row that fails to write is counted and carried on the next row this PROCESS
# writes (``prior_write_failures``), which the verdict reads as INCONCLUSIVE.
# The limit, stated for the verdict to account for: the count lives in memory,
# so a process that never writes again (the one-shot ambient worker, or a
# session that ends right after the failure) loses it. Callers close their
# tally in ``finally``, so an error or cancellation inside the call still
# writes what it collected.

#: ``eval_events.event_type`` for every traversal-telemetry row.
TELEMETRY_EVENT_TYPE = "graph_traverse"
#: Pruned after this many days by the learning scheduler's
#: ``graph_traverse_prune``; comfortably longer than the 14-day verdict window.
TELEMETRY_RETENTION_DAYS = 30
#: Operator kill switch: ``1`` records nothing.
_TELEMETRY_OFF_ENV = "GENESIS_GRAPH_TELEMETRY_DISABLED"
#: Outcomes that break the cutover clock and so carry an event entry.
_CLOCK_BREAKING = frozenset({"fallback", "selection_failed", "error"})

#: Telemetry rows this process failed to write since the last one that landed.
_write_failures = 0
_proc_role: str | None = None


@dataclass
class _Tally:
    caller: str
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


def _process_role(argv: list[str] | None = None) -> str:
    """Which kind of process is traversing, from its original command line."""
    if argv is None:
        argv = list(getattr(sys, "orig_argv", None) or sys.argv)
    names = [os.path.basename(a) for a in argv]
    if "genesis_mcp_server.py" in names:
        rest = argv[names.index("genesis_mcp_server.py") + 1 :]
        if "--server" in rest and rest.index("--server") + 1 < len(rest):
            return f"mcp-{rest[rest.index('--server') + 1]}"
        return "mcp-unknown"
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


def begin_tally(caller: str) -> contextvars.Token | None:
    """Open a tally for one caller call. Returns ``None`` when one is already
    open, in which case this call's traversals join it and ``end_tally`` with
    that ``None`` does nothing."""
    if _TALLY.get() is not None:
        return None
    return _TALLY.set(_Tally(caller=caller))


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
        await _write_tally(tally, db, db_path=db_path)


@asynccontextmanager
async def traversal_tally(
    db: aiosqlite.Connection | None,
    *,
    caller: str,
    db_path: str | None = None,
) -> AsyncIterator[None]:
    """Record every traversal made inside the block as ONE telemetry row."""
    token = begin_tally(caller)
    try:
        yield
    finally:
        await end_tally(token, db, db_path=db_path)


async def note_traversal(db: aiosqlite.Connection, **outcome: str | None) -> None:
    """Record one traversal: into the open tally, or as its own row."""
    if _telemetry_off():
        return
    tally = _TALLY.get()
    if tally is not None:
        tally.add(**outcome)
        return
    standalone = _Tally(caller="direct")
    standalone.add(**outcome)
    await _write_tally(standalone, db)


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
        _proc_role = _process_role()
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
