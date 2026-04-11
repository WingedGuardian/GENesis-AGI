"""Pause / kill switch state for ``GenesisRuntime``.

Pause is a global runtime kill switch that blocks all background
dispatches when set. The state lives in three places at once for
robustness:

- In-memory (``self._paused``, ``self._pause_reason``, ``self._paused_since``)
  for fast checks on the hot path.
- On disk (``~/.genesis/paused.json``) so cross-process consumers
  (the dashboard, the Telegram bridge, the standalone server) can
  flip the switch and have other processes notice on their next
  ``paused`` read.
- On the event bus (``Subsystem.RUNTIME`` ``pause``/``resume`` events)
  for historical visibility in the events table.

This mixin owns all three. The ``paused`` property is the only public
read accessor — it reconciles in-memory state with the on-disk file
on every read so cross-process pauses propagate without an explicit
poll loop.

Pure mixin: no ``__init__``, no methods other than the pause API. The
in-memory attributes are set by ``GenesisRuntime.__init__`` before any
mixin method is callable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("genesis.runtime")


class _PauseStateMixin:
    """Mixin: pause / kill switch state for GenesisRuntime."""

    _PAUSE_FILE = Path.home() / ".genesis" / "paused.json"

    @property
    def paused(self) -> bool:
        # Check file on disk so cross-process pause (dashboard → bridge) works.
        # Only reads file when in-memory state is unpaused (cheap fast path).
        if not self._paused and self._PAUSE_FILE.exists():
            self._restore_pause_state()
        elif self._paused and not self._PAUSE_FILE.exists():
            # Unpaused from another process (dashboard or Telegram)
            self._paused = False
            self._pause_reason = None
            self._paused_since = None
        return self._paused

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    @property
    def paused_since(self) -> datetime | None:
        return self._paused_since

    def set_paused(self, paused: bool, reason: str | None = None) -> None:
        self._paused = paused
        self._pause_reason = reason if paused else None
        self._paused_since = datetime.now(UTC) if paused else None
        self._persist_pause_state()
        logger.info("Genesis %s%s", "PAUSED" if paused else "RESUMED",
                     f" — {reason}" if reason else "")
        # Record pause/resume in events table for historical visibility
        if self._event_bus is not None:
            import asyncio

            async def _emit() -> None:
                try:
                    from genesis.observability.types import Severity, Subsystem

                    await self._event_bus.emit(
                        subsystem=Subsystem.RUNTIME,
                        event_type="pause" if paused else "resume",
                        message=reason or ("Paused" if paused else "Resumed"),
                        severity=Severity.INFO,
                    )
                except Exception:
                    logger.debug("Failed to emit pause event", exc_info=True)

            try:
                asyncio.get_running_loop()
                from genesis.util.tasks import tracked_task

                tracked_task(_emit(), name="pause-event-emit")
            except RuntimeError:
                pass  # No event loop — skip (startup/shutdown edge)

    def _persist_pause_state(self) -> None:
        try:
            if self._paused:
                import json as _json

                self._PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
                self._PAUSE_FILE.write_text(_json.dumps({
                    "paused": True,
                    "reason": self._pause_reason,
                    "since": self._paused_since.isoformat() if self._paused_since else None,
                }))
            else:
                self._PAUSE_FILE.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to persist pause state", exc_info=True)

    def _restore_pause_state(self) -> None:
        try:
            if self._PAUSE_FILE.exists():
                import json as _json

                data = _json.loads(self._PAUSE_FILE.read_text())
                self._paused = data.get("paused", False)
                self._pause_reason = data.get("reason")
                since_raw = data.get("since")
                self._paused_since = datetime.fromisoformat(since_raw) if since_raw else None
                if self._paused:
                    logger.warning(
                        "Genesis starting in PAUSED state (since %s: %s)",
                        self._paused_since, self._pause_reason,
                    )
        except (OSError, ValueError):
            logger.warning("Failed to restore pause state", exc_info=True)
