"""Box-wide advisory lock that serializes pytest runs on one machine.

Concurrent test suites thrash this swapless box — they contend for the same
CPUs and RAM as the live Genesis services and repeatedly OOM-killed a run
mid-suite. A PreToolUse hook (``scripts/hooks/concurrent_test_guard.py``)
refuses a *Claude-Code Bash* pytest call while another suite runs, but it can
only see commands routed through that tool. Cron jobs, plain SSH shells,
background sessions and sibling worktrees all reach pytest without passing it,
which is why ``tests/conftest.py`` previously worked around a *symptom* of the
overlap (per-pid ``basetemp`` leaves) rather than the overlap itself.

This module is the actual mutual exclusion, and it is the single source of
truth: the hook and its ``--wait`` mode both PROBE this lock rather than
guessing from process command lines, so the layer that blocks and the layer
that waits cannot disagree.

Two kinds of caller acquire it:

* ``tests/conftest.py::pytest_configure`` — covers every run of *this* repo's
  suite, from any launcher and any worktree, because nothing reaches those
  tests without loading that conftest.
* ``genesis.eval.gauntlet`` — acquires explicitly, because it scores *foreign*
  fixture projects: it runs pytest with a ``cwd`` under ``~/tmp/gauntlet``, so
  those projects have their own rootdir and this repo's conftest never loads
  for them. Without an explicit acquire it would be entirely ungoverned while
  still consuming the box for up to its per-fixture timeout.

Design constraints, in priority order:

1. **Fail OPEN, always.** This is a resource governor, not a correctness gate.
   Every failure mode — unreadable lock dir, EACCES, a corrupt holder record,
   an unrecognised env value, an unexpected OSError — resolves to "run the
   tests". A bug here must never be able to stop the suite from running.
2. **Never block by default.** Acquisition is ``LOCK_EX | LOCK_NB``. An
   interactive run that loses reports the holder and exits immediately, so the
   caller stays responsive and can wait deliberately. Only a caller that opts
   in (``GENESIS_PYTEST_LOCK_WAIT``, or ``wait=True`` in-process) blocks, which
   is the right shape for a background eval that would rather queue than fail.
3. **No stale locks.** ``flock`` is released by the kernel when the holding
   process dies, SIGKILL included, so a crashed run cannot wedge the box. The
   lock file is deliberately never unlinked: an unlink races a contender that
   has already opened it, and a leftover file from a dead holder correctly
   reads as free.
4. **Inert off a real install.** The lock lives under ``~/.genesis``; when that
   directory does not exist (CI containers, a fresh checkout) the whole
   mechanism no-ops rather than creating state.

Why not :class:`genesis.util.process_lock.ProcessLock`: its ``__enter__`` calls
``sys.exit()`` on contention (wrong for a caller that wants to queue or report)
and its ``__exit__`` unlinks the lock file (racy — see constraint 3). Its
``is_locked`` probe is the right shape, and the hook's probe matches it
deliberately; the hook cannot import ``genesis`` at all, so it carries its own
copy, pinned by a test.

Environment levers:

``GENESIS_PYTEST_LOCK=0``
    Disable entirely — the escape hatch for a deliberate concurrent run.
``GENESIS_PYTEST_LOCK_WAIT=1``
    Block until the lock frees instead of failing fast.
``GENESIS_PYTEST_LOCK_WAIT_TIMEOUT=<seconds>``
    Bound on that wait (default ``DEFAULT_WAIT_TIMEOUT``).
``GENESIS_PYTEST_LOCK_PATH``
    Point the lock at an explicit file instead of the default.
``GENESIS_PYTEST_LOCK_HELD``
    Set by the holder and inherited by children, so a pytest spawned *inside* a
    locked run can never deadlock against its own parent.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import math
import os
import sys
import time
from pathlib import Path

# Distinct exit status for "another pytest holds the box lock", mirroring
# ``process_lock.EXIT_ALREADY_RUNNING`` so the two read the same way in logs.
# Deliberately not 1: that is pytest's "tests failed", and conflating a
# scheduling refusal with a real failure would send a caller debugging.
EXIT_LOCK_HELD = 200

#: Ceiling on an opted-in blocking wait. Per the project timeout policy this is
#: the 2-hour floor: long enough that no legitimate suite is ever cut short,
#: bounded so a wedged holder cannot strand a caller forever.
DEFAULT_WAIT_TIMEOUT = 7200.0

#: Absolute cap on any caller-supplied wait. A test run still unfinished after a
#: day is wedged by any definition, and an unbounded wait sits on the critical
#: path of every run — so ``inf`` (reachable via ``--wait=inf`` or a mistyped
#: ``1e999``) must not be representable as a deadline.
MAX_WAIT_TIMEOUT = 86400.0

#: Poll interval. flock has no timed acquire, so a bounded poll is the
#: condition-based wait; 2s is far below any real suite's runtime.
_POLL_SECONDS = 2.0

#: Holder records are 3 short lines; cap the read so a redirected lock path
#: cannot make a blocked run slurp an arbitrary file.
_RECORD_READ_LIMIT = 4096

DISABLE_ENV = "GENESIS_PYTEST_LOCK"
WAIT_ENV = "GENESIS_PYTEST_LOCK_WAIT"
WAIT_TIMEOUT_ENV = "GENESIS_PYTEST_LOCK_WAIT_TIMEOUT"
HELD_ENV = "GENESIS_PYTEST_LOCK_HELD"
PATH_ENV = "GENESIS_PYTEST_LOCK_PATH"

_FALSEY = {"0", "off", "false", "no", "n"}
_TRUTHY = {"1", "on", "true", "yes", "y"}

#: How a blocked caller is told to wait. Duplicated in the hook (which cannot
#: import ``genesis``) and pinned equal by a test.
WAIT_COMMAND = "python3 scripts/hooks/concurrent_test_guard.py --wait"


def _env_flag(name: str, default: bool, on_unknown: bool | None = None) -> bool:
    """A tri-state env flag: unset → *default*, else truthy/falsey by word.

    An UNRECOGNISED value resolves to *on_unknown* (default: *default*) and says
    so on stderr. That matters for the disable hatch: someone reaching for it
    types ``GENESIS_PYTEST_LOCK=`` (the shell idiom for blanking a variable),
    ``n``, or ``disabled``, and silently leaving the lock armed would be a
    documented escape hatch that fails CLOSED — the exact direction constraint 1
    forbids.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSEY or token == "":
        return False
    resolved = default if on_unknown is None else on_unknown
    print(
        f"[pytest_lock] {name}={raw!r} not understood — treating it as "
        f"{'set' if resolved else 'unset'}.",
        file=sys.stderr,
    )
    return resolved


def default_lock_path() -> Path | None:
    """The box-wide lock file, or ``None`` when this is not a real install.

    Anchored on ``~/.genesis`` EXISTING rather than creating it: on a CI runner
    or a bare checkout there is no install to govern, and silently materialising
    install state as a side effect of running tests would be wrong. The
    ``locks/`` leaf itself is created on demand, matching every other consumer.
    """
    override = os.environ.get(PATH_ENV)
    if override:
        # An explicit scope for the lock. Legitimate config (a second install,
        # a narrower scope) and what lets a test exercise contention without
        # contending for the REAL box lock its own session is holding.
        return Path(override)
    try:
        base = Path.home() / ".genesis"
        if not base.is_dir():
            return None
        return base / "locks" / "pytest.lock"
    except (OSError, RuntimeError):
        # No resolvable home (RuntimeError from Path.home) → no lock. Fail open.
        return None


def _format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _read_holder(path: Path) -> str:
    """A human description of the current holder, best-effort.

    The record is written by the holder as ``pid\\nstarted\\nargv``. A contender
    can read it while the holder is mid-rewrite, so every field is parsed
    defensively — a partial or corrupt record degrades to a generic description
    rather than raising into the caller's fail-open path.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            # Bounded: three short lines are all that is ever written, and the
            # path is env-redirectable.
            raw = fh.read(_RECORD_READ_LIMIT)
    except OSError:
        return "another pytest run (holder details unavailable)"
    lines = raw.split("\n")
    pid = lines[0].strip() if lines else ""
    if not pid.isdigit():
        return "another pytest run (holder details unavailable)"
    detail = f"pid {pid}"
    if len(lines) > 1:
        with contextlib.suppress(TypeError, ValueError):
            detail += f", running {_format_age(time.time() - float(lines[1]))}"
    if len(lines) > 2 and lines[2].strip():
        detail += f"\n  command: {lines[2].strip()}"
    return detail


class BoxLock:
    """The outcome of an acquisition attempt. Never raises; never blocks unasked.

    ``blocked`` is the only field a caller must honour: True means "do not run".
    Every other outcome — acquired, disabled, no install, inner run, internal
    error — is False, because the fail-open direction is to run the tests.
    """

    def __init__(
        self,
        *,
        blocked: bool = False,
        message: str = "",
        fd: int | None = None,
        path: Path | None = None,
        owns_held_env: bool = False,
    ) -> None:
        self.blocked = blocked
        self.message = message
        self._fd = fd
        self._path = path
        self._owns_held_env = owns_held_env

    @property
    def acquired(self) -> bool:
        """Whether this process actually holds the lock (vs. no-opped)."""
        return self._fd is not None

    def release(self) -> None:
        """Release if held. Idempotent, and safe to call after any outcome.

        The lock file is intentionally left in place — see the module docstring.
        """
        if self._owns_held_env and os.environ.get(HELD_ENV) == str(os.getpid()):
            os.environ.pop(HELD_ENV, None)
            self._owns_held_env = False
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        # Closing the fd releases the lock regardless, so an unlock failure is
        # not load-bearing — but unlock first so the release is prompt.
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def __enter__(self) -> BoxLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _write_holder_record(fd: int) -> None:
    """Stamp pid / start time / argv so a contender can name the holder.

    Written with ``pwrite`` and truncated to length AFTERWARDS, so the file is
    never momentarily empty: truncate-then-write leaves a window in which a
    contender reads nothing. (A torn record is handled anyway — the decision
    never depends on the parse — but a needless window is still a window.)
    Best-effort: failing to describe ourselves must not cost us the lock we
    already hold.
    """
    try:
        argv = " ".join(sys.argv)[:500]  # bounded — argv can be enormous
        record = f"{os.getpid()}\n{time.time()}\n{argv}\n".encode()
        os.pwrite(fd, record, 0)
        os.ftruncate(fd, len(record))
    except OSError:
        pass


def acquire(
    *,
    lock_path: Path | None = None,
    wait: bool | None = None,
    timeout: float | None = None,
) -> BoxLock:
    """Attempt the box-wide pytest lock. Never raises.

    Args:
        lock_path: override the lock file (tests pass a tmp path — never let a
            test contend for the real box lock, which its own session holds).
        wait: block until free instead of failing fast. Defaults to the
            ``GENESIS_PYTEST_LOCK_WAIT`` env flag (off).
        timeout: bound on that wait; defaults to ``GENESIS_PYTEST_LOCK_WAIT_TIMEOUT``
            or ``DEFAULT_WAIT_TIMEOUT``. Clamped to ``MAX_WAIT_TIMEOUT``.

    Returns:
        A :class:`BoxLock`. Check ``.blocked``; call ``.release()`` when done.
    """
    try:
        return _acquire_inner(lock_path=lock_path, wait=wait, timeout=timeout)
    except Exception:  # noqa: BLE001 — fail-open is the whole contract
        return BoxLock()


def _acquire_inner(
    *,
    lock_path: Path | None,
    wait: bool | None,
    timeout: float | None,
) -> BoxLock:
    # Unrecognised → treat as DISABLED (fail open): a mistyped escape hatch must
    # not leave the governor armed. See _env_flag.
    if not _env_flag(DISABLE_ENV, True, on_unknown=False):
        return BoxLock()

    # An inner pytest inherits the holder's env. Taking the same lock would
    # deadlock a run against its own parent, so descendants always no-op.
    if os.environ.get(HELD_ENV):
        return BoxLock()

    path = lock_path or default_lock_path()
    if path is None:
        return BoxLock()  # not a real install (CI, bare checkout) — inert

    path.parent.mkdir(parents=True, exist_ok=True)
    # 0o600, not 0o644: the holder record carries the running pytest's argv,
    # which on a multi-user host would otherwise expose another user's -k
    # filters and paths (the file is deliberately never unlinked, so it
    # outlives the run). O_NOFOLLOW because the lock is always a regular
    # file we create; a symlink there is a misconfiguration, and the
    # resulting ELOOP lands on the fail-open path.
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

    if wait is None:
        # Unrecognised → do NOT wait: hanging is the worse direction here.
        wait = _env_flag(WAIT_ENV, False)
    timeout = _sanitize_timeout(timeout if timeout is not None else _env_timeout())

    deadline = time.monotonic() + timeout if wait else None
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                # An I/O-level failure is not contention. Fail OPEN: a governor
                # that cannot read its own lock must not stop the suite.
                os.close(fd)
                return BoxLock()
            if deadline is None:
                holder = _read_holder(path)
                os.close(fd)
                return BoxLock(blocked=True, message=_contention_message(holder))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                holder = _read_holder(path)
                os.close(fd)
                return BoxLock(
                    blocked=True,
                    message=_timeout_message(holder, timeout),
                )
            # Clamp to the deadline: a 5s wait must report at 5s, not overshoot
            # by a whole poll interval.
            time.sleep(min(_POLL_SECONDS, remaining))
            continue
        break

    _write_holder_record(fd)
    os.environ[HELD_ENV] = str(os.getpid())
    return BoxLock(fd=fd, path=path, owns_held_env=True)


def _sanitize_timeout(value: float) -> float:
    """A finite, positive, bounded wait. Never ``inf`` — see MAX_WAIT_TIMEOUT."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_WAIT_TIMEOUT
    return min(value, MAX_WAIT_TIMEOUT)


def _env_timeout() -> float:
    raw = os.environ.get(WAIT_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_WAIT_TIMEOUT
    try:
        return _sanitize_timeout(float(raw.strip()))
    except (AttributeError, ValueError):
        return DEFAULT_WAIT_TIMEOUT


def _contention_message(holder: str) -> str:
    return (
        "BLOCKED: another pytest run holds the box-wide test lock.\n"
        f"  holder: {holder}\n"
        "Concurrent suites contend for the CPU and RAM of the live Genesis "
        "services on a swapless box, and take far longer than running in turn.\n"
        "Wait for it to finish — run this as its OWN command, NOT chained "
        "before your pytest with '&&':\n"
        f"  {WAIT_COMMAND}\n"
        "then re-run your pytest. (Chaining is rejected: the concurrent-test "
        "guard sees the pytest in the same command and blocks the whole thing, "
        "so the wait never runs.)\n"
        f"Deliberate concurrent run: {DISABLE_ENV}=0 pytest ...\n"
        f"Queue instead of failing (background jobs): {WAIT_ENV}=1 pytest ..."
    )


def _timeout_message(holder: str, timeout: float) -> str:
    return (
        f"BLOCKED: waited {int(timeout)}s for the box-wide pytest lock and it is "
        "still held.\n"
        f"  holder: {holder}\n"
        "The holder may be wedged — check it before overriding with "
        f"{DISABLE_ENV}=0."
    )
