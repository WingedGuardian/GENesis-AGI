"""A process-level hard stop for a hook that must finish before Claude Code kills it.

Claude Code kills a hook at its configured timeout and DISCARDS everything the
hook printed. A deadline checked between steps cannot stop a step that is
already running: a SQLite query, a commit, a cold import or a file read can each
run for seconds while the disk is stalled. MEASURED 2026-10-07: three
memory-hook kills (10.0, 10.6 and 12.5 s), each after the hook had passed its own
8 s soft deadline inside such a step.

``arm(seconds, notice)`` starts a daemon timer. If the hook has not called
``disarm()`` by then, the timer:

1. takes ``STDOUT_LOCK`` (waiting at most 0.2 s);
2. sets fds 1 and 2 non-blocking, so no write below, the flush included, can
   block the exit; then, only if it got the lock, flushes ``sys.stdout`` and
   ends any half-written line;
3. writes ``notice()`` to stdout (only if it got the lock and the callback
   returns text);
4. writes the main thread's stack (``co_name:lineno``, no file I/O) to stderr,
   which Claude Code keeps in the transcript's ``hook_success`` row;
5. calls ``os._exit(0)``.

Why this works while the main thread is blocked: CPython releases the GIL around
sqlite3 open/prepare/step/commit and around blocking file and pipe reads, so the
timer thread runs. MEASURED by a scratch harness (timer at 0.5 s): exit at
0.56-0.62 s with the main thread in a sqlite query, a sqlite busy-wait, an
import holding the import lock, a pipe read, a sleep or a CPU loop. What it
cannot bound: a thread in uninterruptible disk sleep (an fsync) delays process
exit until that I/O returns, and a C call that holds the GIL delays the timer.

The two continuations that were measured to HANG are why the steps are ordered
as they are: flushing ``sys.stdout`` without the lock blocks on the buffer's own
lock (no timeout), and a blocking write to a full stdout pipe blocks forever,
which includes the flush itself if it runs before the fds are non-blocking.
Nothing here imports at fire time: the main thread may hold the import lock.

STDLIB-ONLY. Shared by hooks under ``scripts/``; it reads no Genesis state.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable

#: Held by every write ``LockedStdout`` makes, and by the timer before it touches
#: stdout, so the timer never flushes or writes in the middle of the main
#: thread's write.
STDOUT_LOCK = threading.Lock()

#: An age above this is not believed. Claude Code reaps a timed-out memory hook
#: a few seconds past its 10 s limit (MEASURED 2026-10-07: 10,033 and 12,523 ms),
#: so a larger reading means the two clocks disagree, not that the process is
#: that old; callers then fall back to their full budget.
PROCESS_AGE_MAX_S = 30.0


def process_age_s() -> float | None:
    """Seconds since this process started, or None when it cannot be read.

    The hook launchers ``exec`` into Python, so this pid's start time is when the
    launcher started. If Claude Code runs the hook through a shell that forks
    rather than execs, it is slightly later than the spawn, so the age can only
    be under-counted: the deadlines never fire early, at most slightly late.
    (The repo's launcher does ``exec``, so today the pid is the spawn.)
    Measured against CLOCK_BOOTTIME, the
    clock the kernel's start time is counted on. /proc/uptime is NOT used: lxcfs
    virtualises it in a container, and it read 28 s behind the process start
    time on a live box.
    """
    try:
        with open("/proc/self/stat") as fh:
            raw = fh.read()
        # comm (field 2) may contain spaces and parentheses; start after its last ")".
        fields = raw[raw.rindex(")") + 2 :].split()
        started = int(fields[19]) / os.sysconf("SC_CLK_TCK")  # field 22: starttime
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - started
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    if not 0.0 <= age <= PROCESS_AGE_MAX_S:
        return None
    return age


_DONE = threading.Event()
_LINE_OPEN = False  # the last write to stdout did not end with a newline


class LockedStdout:
    """A stdout stand-in whose writes hold ``STDOUT_LOCK``.

    Resolves ``sys.stdout`` at call time, so test capture and redirection keep
    working. Pass it as ``BoundedStdout(stream=...)``.
    """

    def write(self, text: str) -> int:
        """Write under the lock and record whether a line was left open."""
        global _LINE_OPEN
        with STDOUT_LOCK:
            written = sys.stdout.write(text)
            if text:
                _LINE_OPEN = not text.endswith("\n")
            return written

    def flush(self) -> None:
        """Flush under the lock."""
        with STDOUT_LOCK:
            sys.stdout.flush()


def _main_stack(max_frames: int = 40) -> str:
    """The main thread's stack as ``file:func:line`` frames, innermost first."""
    frames = sys._current_frames()
    frame = frames.get(threading.main_thread().ident or -1)
    parts: list[str] = []
    while frame is not None and len(parts) < max_frames:
        code = frame.f_code
        parts.append(f"{code.co_filename.rsplit('/', 1)[-1]}:{code.co_name}:{frame.f_lineno}")
        frame = frame.f_back
    return " <- ".join(parts) or "unavailable"


def _write_all(fd: int, data: bytes) -> None:
    """Write without ever blocking; drop what does not fit."""
    with contextlib.suppress(OSError):
        os.write(fd, data)


def _fire(seconds: float, label: str, notice: Callable[[], str | None]) -> None:
    if _DONE.is_set():
        return
    got = STDOUT_LOCK.acquire(timeout=0.2)
    try:
        for fd in (1, 2):
            with contextlib.suppress(OSError):
                os.set_blocking(fd, False)
        if got:
            # A flush IS a write: on a full pipe it would block forever, so it
            # runs only after the fds are non-blocking (it then fails fast and
            # what did not fit is dropped).
            with contextlib.suppress(Exception):
                sys.stdout.flush()
        if got:
            try:
                text = notice()
            except Exception:
                text = None
            if text:
                lead = "\n" if _LINE_OPEN else ""
                _write_all(1, (lead + text + "\n").encode("utf-8", "replace"))
        _write_all(
            2,
            (f"[{label}] hard stop fired {seconds:.1f}s after arming; main thread: "
             f"{_main_stack()}\n").encode(
                "utf-8", "replace"
            ),
        )
    finally:
        os._exit(0)


def arm(
    seconds: float,
    notice: Callable[[], str | None],
    *,
    label: str = "hook",
) -> threading.Timer:
    """Start the hard stop. ``notice`` runs on the timer thread at fire time and
    returns the stdout line to add, or None for none. It must not import."""
    _DONE.clear()
    timer = threading.Timer(max(0.0, seconds), _fire, args=(seconds, label, notice))
    timer.daemon = True
    timer.start()
    return timer


def arm_from_spawn(
    budget_s: float,
    notice: Callable[[], str | None],
    *,
    label: str = "hook",
) -> threading.Timer:
    """Arm the hard stop to fire ``budget_s`` after this PROCESS started.

    An unreadable age counts as zero: the timer then fires ``budget_s`` from now,
    which is never earlier than intended.
    """
    return arm(budget_s - (process_age_s() or 0.0), notice, label=label)


def disarm(timer: threading.Timer | None) -> None:
    """Stop the hard stop. Safe to call more than once, or with None.

    ``cancel()`` alone cannot stop a callback that has already started, so the
    callback also checks ``_DONE`` first.
    """
    _DONE.set()
    if timer is not None:
        timer.cancel()
