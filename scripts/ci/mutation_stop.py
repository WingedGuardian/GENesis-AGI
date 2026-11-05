"""How a mutation sweep stops: one recorded signal, and the steps it waits for.
The guarantees are in `mutation_sweep.py`'s docstring."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import Iterator

#: The signal that stopped this sweep, if any: a fact, checked between cases and
#: before the tally, because a raise is not enough. Python discards an exception
#: raised inside a `__del__`, so a signal landing in one would otherwise read as a
#: clean sweep at exit 0.
_stop_signal: int | None = None
_held = 0


def _raise_if_stopped() -> None:
    if _stop_signal is not None:
        raise SystemExit(128 + _stop_signal)


@contextlib.contextmanager
def _hold(*, then_stop: bool = True) -> Iterator[None]:
    """A signal arriving inside this waits for its end, however long the step
    takes. For a step a raise would damage: one part way through `git worktree add`
    leaves an entry locked "initializing" in the caller's repository, one in cleanup
    abandons it. ``then_stop=False`` leaves the stop to the next check: a raise out
    of a copy's cleanup would discard the result its case has just reached."""
    global _held
    _held += 1
    try:
        yield
    finally:
        _held -= 1
        if then_stop and not _held:
            _raise_if_stopped()


def _stopped(signum: int, _frame: object) -> None:
    """Record the stop, say so once, and unwind unless a held step is running. The
    unwinding is what kills the child's group and removes the copy. SystemExit,
    because the per-case boundary must not catch it. Raw `os.write`: a signal can
    land inside a `print`, which is not reentrant. A closed stream must not turn
    the stop into an error at whatever it interrupted."""
    global _stop_signal
    if _stop_signal is None:
        _stop_signal = signum
        with contextlib.suppress(OSError, ValueError):
            os.write(
                sys.stdout.fileno(),
                f"  mutation-sweep: stopped by signal {signum}. Only the results printed "
                "are verdicts; the other cases have none.\n".encode(),
            )
    if not _held:
        _raise_if_stopped()


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def _catch_stop_signals() -> dict:
    """Install `_stopped`, leaving alone a signal the caller ignored (`nohup`).
    Returns the handlers it replaced."""
    return {
        each: signal.signal(each, _stopped)
        for each in _STOP_SIGNALS
        if signal.getsignal(each) != signal.SIG_IGN
    }
