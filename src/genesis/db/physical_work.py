"""Private physical completion records for request-owned synchronous work."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextvars import copy_context
from typing import Any

logger = logging.getLogger(__name__)


class UnconfirmedWork(RuntimeError):
    """Submission or proxy state cannot establish a physical terminal."""


class PhysicalWork:
    """A proxy is only an observer; the wrapper owns the terminal and outcome.

    ``submit`` may raise after enqueue, leaving no proxy but possibly real work.
    The independent record remains owned until its wrapper actually terminates.
    """

    def __init__(self, fn, args, kwargs):
        self._loop = asyncio.get_running_loop()
        self._context = copy_context()
        self._fn, self._args, self._kwargs = fn, args, kwargs
        # Starting and abandoning are decided under one lock: once the owner
        # settles unstarted work as unconfirmed, the work can no longer start.
        self._start_lock = threading.Lock()
        self._abandoned = False
        self._started = threading.Event()
        self._complete = threading.Event()
        self._terminal = self._loop.create_future()
        self._proxy: asyncio.Future | None = None
        self._value: Any = None
        self._error: BaseException | None = None

    @property
    def complete(self) -> bool:
        return self._complete.is_set()

    def submit(self) -> None:
        self._proxy = self._loop.run_in_executor(None, self._run)

    def _notify(self) -> None:
        if not self._terminal.done():
            self._terminal.set_result(None)

    def _abandon_if_unstarted(self) -> bool:
        with self._start_lock:
            if self._started.is_set():
                return False
            self._abandoned = True
            return True

    def _run(self) -> None:
        with self._start_lock:
            if self._abandoned:
                return
            self._started.set()
        try:
            self._value = self._context.run(self._fn, *self._args, **self._kwargs)
        except BaseException as exc:
            self._error = exc
        finally:
            self._complete.set()
            try:
                self._loop.call_soon_threadsafe(self._notify)
            except RuntimeError:
                logger.error("Physical resource work finished after its owner loop closed")

    async def wait(self) -> None:
        """Settle known physical work, never await an unproven producer."""
        if self.complete:
            return
        proxy = self._proxy
        if proxy is not None:
            await asyncio.wait({proxy, self._terminal}, return_when=asyncio.FIRST_COMPLETED)
            if self.complete:
                return
            if proxy.done() and not proxy.cancelled():
                proxy.exception()  # observe an executor failure, not physical success
        if self._terminal.done() or self._abandon_if_unstarted():
            # Unstarted work is abandoned here, so a submission that raised
            # after enqueue cannot run against resources its owner then closes.
            raise UnconfirmedWork("Physical resource completion is unconfirmed")
        # A cancelled proxy cannot terminate an already-running synchronous body.
        await asyncio.shield(self._terminal)
        if not self.complete:
            raise UnconfirmedWork("Physical terminal has no confirmed completion")

    async def result(self) -> Any:
        await self.wait()
        if self._error is not None:
            raise self._error
        return self._value
