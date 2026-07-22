"""Tracked async tasks — observable alternatives to bare asyncio.create_task().

Every bare ``asyncio.create_task()`` is a silent-failure factory: if the
coroutine raises, the exception is swallowed by the event loop with only a
``Task exception was never retrieved`` warning on GC.  ``tracked_task()``
adds a ``done_callback`` that logs the error at ERROR level and optionally
emits a structured event to the Genesis event bus.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from genesis.observability.events import GenesisEventBus
    from genesis.observability.types import Subsystem

_default_logger = logging.getLogger("genesis.tasks")

# Process-wide fallback bus (reflex arc PR1). Most tracked_task call sites
# never passed event_bus=, so their failures died in the logger — zero
# task.failed events in 36 days of history. The default bus makes every
# site (current and future) emit without per-call-site plumbing. Installed
# by runtime init, gated on reflex ingestion config; an explicit event_bus
# argument always wins.
_default_event_bus: GenesisEventBus | None = None

# Frames deeper than this carry diminishing identity and bloat the payload.
_FRAME_TAIL_LEN = 3


def set_default_event_bus(bus: GenesisEventBus | None) -> None:
    """Install (or clear, with ``None``) the process-wide fallback bus."""
    global _default_event_bus
    _default_event_bus = bus


def _normalized_frames(exc: BaseException) -> list[str]:
    """Render the traceback tail as stable ``relpath:funcname`` strings.

    - keeps only frames inside the genesis package (``/genesis/`` path
      segment — package-relative, so it works on any install/CI checkout);
      if none match, keeps the whole traceback with basename paths
    - takes the deepest ``_FRAME_TAIL_LEN`` frames (closest to the raise)
    - carries NO line numbers: they shift on every unrelated commit and
      would split one bug into many fingerprints across deploys
    """
    tb = exc.__traceback__
    if tb is None:
        return []
    frames = traceback.extract_tb(tb)
    genesis_frames = [f for f in frames if "/genesis/" in f.filename]
    selected = (genesis_frames or list(frames))[-_FRAME_TAIL_LEN:]
    rendered = []
    for frame in selected:
        filename = frame.filename
        idx = filename.rfind("/genesis/")
        rel = filename[idx + len("/genesis/") :] if idx != -1 else filename.rsplit("/", 1)[-1]
        rendered.append(f"{rel}:{frame.name}")
    return rendered


def tracked_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
    event_bus: GenesisEventBus | None = None,
    subsystem: Subsystem | None = None,
    logger: logging.Logger | None = None,
) -> asyncio.Task:
    """Create an asyncio task with automatic error observation.

    Parameters
    ----------
    coro:
        The coroutine to schedule.
    name:
        Human-readable task name (used in log messages and event details).
    event_bus:
        If provided, emit an ERROR event when the task fails.
    subsystem:
        Subsystem for the event (defaults to HEALTH).
    logger:
        Logger instance for error logging.  Falls back to
        ``genesis.tasks`` if not provided.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(
        _make_done_callback(
            task_name=name or "unnamed",
            event_bus=event_bus,
            subsystem=subsystem,
            log=logger or _default_logger,
        )
    )
    return task


def _make_done_callback(
    *,
    task_name: str,
    event_bus: GenesisEventBus | None,
    subsystem: Subsystem | None,
    log: logging.Logger,
):
    """Return a done-callback closure that logs + emits on task failure."""

    def _on_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return

        log.error(
            "Background task %r failed: %s",
            task_name,
            exc,
            exc_info=exc,
        )

        # Explicit per-call bus wins; the module default covers the ~60
        # call sites that never passed one. Read at fire time (not closure
        # creation) so tasks started before runtime init still emit.
        bus = event_bus if event_bus is not None else _default_event_bus
        if bus is not None:
            emit_sync(
                bus,
                subsystem=subsystem,
                severity_str="ERROR",
                event_type="task.failed",
                message=f"Background task {task_name!r} failed: {exc}",
                task_name=task_name,
                error=str(exc),
                error_type=type(exc).__name__,
                error_frames=_normalized_frames(exc),
            )

    return _on_done


def emit_sync(
    event_bus: GenesisEventBus,
    *,
    subsystem: Subsystem | None = None,
    severity_str: str = "ERROR",
    event_type: str,
    message: str,
    **details: object,
) -> None:
    """Schedule an async event emission from synchronous code.

    Only works when an event loop is running (which it always is in Genesis).
    Fire-and-forget — if the emit itself fails, the error is logged.
    This is intentionally one level of fire-and-forget: it logs a failure
    of a fire-and-forget task, so the recursion stops here.
    """
    from genesis.observability.types import Severity
    from genesis.observability.types import Subsystem as Sub

    sev = Severity[severity_str] if severity_str in Severity.__members__ else Severity.ERROR
    sub = subsystem or Sub.HEALTH

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            event_bus.emit(sub, sev, event_type, message, **details),
            name="emit-sync-relay",
        )
    except RuntimeError:
        _default_logger.warning(
            "Cannot emit event (no loop): %s/%s: %s",
            sub,
            event_type,
            message,
        )
