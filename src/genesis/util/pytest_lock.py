"""Box-wide advisory lock that serializes pytest runs on one machine.

Concurrent test suites thrash this swapless box — they contend for the same
CPUs and RAM as the live Genesis services and repeatedly OOM-killed a run
mid-suite. ``tests/conftest.py`` previously worked around a *symptom* of the
overlap (per-pid ``basetemp`` leaves) rather than the overlap itself, because
nothing serialized the runs: cron jobs, plain SSH shells, background sessions
and sibling worktrees all reach pytest independently.

This module is the actual mutual exclusion, and it is the only layer that
decides CONCURRENCY — whether a run may proceed *now*. An earlier design also
had a PreToolUse hook decide that from its own reading of the command line; two
layers interpreting different things drifted apart five separate times (the hook
refused the very overrides this module's message prescribes, read an env prefix
on an unrelated shell segment as an opt-out, and disagreed about unrecognised
values and about the lock path). That layer is gone, and
``scripts/pytest_lock_wait.py`` replaced it with a pure CLI that only asks this
lock "free yet?".

A DIFFERENT axis is still decided elsewhere, deliberately:
``scripts/hooks/full_suite_guard.py`` is a PreToolUse hook that refuses a
whole-directory run (scope, not timing) with its own ``# full-suite-ok``
override and its own exit status. That is not the drift class — the two never
decide the same question — but if anything ever starts deciding CONCURRENCY
outside this module, that class is reopened.

Two kinds of caller acquire it:

* ``tests/conftest.py::pytest_configure`` — covers every run of *this* repo's
  suite, from any launcher and any worktree, because loading that conftest is
  how those tests are reached. ``--noconftest`` skips it and so escapes the
  lock; that is a deliberate non-goal rather than a hole, since this governs
  resources and is explicitly not a security boundary (constraint 1).
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
``is_locked`` probe is the right shape, and the probe in
``scripts/pytest_lock_wait.py`` matches it deliberately: that CLI must run
without ``genesis`` importable (from a bare checkout, from cron, from any
shell), so it carries its own copy.

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
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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

def _wait_command() -> str:
    """The command a blocked caller should run, as an ABSOLUTE path.

    A repo-relative path is wrong the moment pytest is launched from a
    subdirectory — ``cd tests && pytest …`` would resolve it to
    ``tests/scripts/…`` and fail with file-not-found. The lock governs runs from
    any working directory, so the advice has to as well. Falls back to the
    relative form only when the script cannot be located (an installed package
    without the repo alongside it), which is better than advertising a path that
    is definitely wrong.
    """
    try:
        script = Path(__file__).resolve().parents[3] / "scripts" / "pytest_lock_wait.py"
        if script.is_file():
            return f"python3 {script}"
    except (OSError, IndexError):
        pass
    return "python3 scripts/pytest_lock_wait.py"


#: How a blocked caller is told to wait.
WAIT_COMMAND = _wait_command()


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
    # State the VALUE the flag took, never "set"/"unset": for the disable
    # lever, resolving to False means DISABLED, whereas *unset* means armed —
    # so the old wording told an operator reaching for the escape hatch the
    # exact opposite of what had happened.
    print(
        f"[pytest_lock] {name}={raw!r} not understood — resolving it to "
        f"{'true' if resolved else 'false'}.",
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
    already hold — so this suppresses Exception, not just OSError.

    Both halves of that are deliberate. ``errors="replace"`` keeps the RECORD
    from being the thing that fails: a non-UTF-8 byte in a filename arrives via
    surrogateescape, and a strict encode raised UnicodeEncodeError — a
    ValueError, which the old ``except OSError`` did not catch, so the caller's
    fail-open hid a stranded flock. Replacing keeps a readable holder line
    instead of losing it. The broad suppression stays anyway: it is belt to that
    braces, and this function must never cost us the lock for any reason.
    """
    try:
        argv = " ".join(sys.argv)[:500]  # bounded — argv can be enormous
        record = f"{os.getpid()}\n{time.time()}\n{argv}\n".encode(
            errors="replace"
        )
        os.pwrite(fd, record, 0)
        os.ftruncate(fd, len(record))
    except Exception:
        pass


def acquire(
    *,
    lock_path: Path | None = None,
    wait: bool | None = None,
    timeout: float | None = None,
    export_env: bool = True,
    cancel: threading.Event | None = None,
) -> BoxLock:
    """Attempt the box-wide pytest lock. Never raises.

    Args:
        lock_path: override the lock file (tests pass a tmp path — never let a
            test contend for the real box lock, which its own session holds).
        wait: block until free instead of failing fast. Defaults to the
            ``GENESIS_PYTEST_LOCK_WAIT`` env flag (off).
        timeout: bound on that wait; defaults to ``GENESIS_PYTEST_LOCK_WAIT_TIMEOUT``
            or ``DEFAULT_WAIT_TIMEOUT``. Clamped to ``MAX_WAIT_TIMEOUT``.
        export_env: publish ``HELD_ENV`` into ``os.environ`` so CHILD processes
            inherit it. Correct for a pytest run, whose children are its own —
            but WRONG for a long-lived process that spawns unrelated children:
            genesis-server dispatches Claude-Code sessions with
            ``env = dict(os.environ)``, so a session started during a gauntlet's
            hold would inherit the flag and silently disable the lock for its
            whole multi-hour life. Such callers pass False and set the variable
            on the one child that needs it.
        cancel: an event that aborts a blocking wait promptly. Without it a
            cancelled caller cannot stop the worker: threads are not
            interruptible, so the wait would run to its full timeout and take
            the lock nobody is waiting for any more.

    Returns:
        A :class:`BoxLock`. Check ``.blocked``; call ``.release()`` when done.
    """
    try:
        return _acquire_inner(
            lock_path=lock_path,
            wait=wait,
            timeout=timeout,
            export_env=export_env,
            cancel=cancel,
        )
    except Exception:
        # Fail-open is the contract, but silence is not: a governor that fails
        # open on every run forever with no signal is indistinguishable from one
        # that works.
        logger.warning("pytest box lock unavailable — running unserialized", exc_info=True)
        return BoxLock()


def _acquire_inner(
    *,
    lock_path: Path | None,
    wait: bool | None,
    timeout: float | None,
    export_env: bool = True,
    cancel: threading.Event | None = None,
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
            # by a whole poll interval. Waiting on the cancel event rather than
            # sleeping makes the abort prompt — a thread cannot be interrupted,
            # so a plain sleep would run the full timeout out after the caller
            # has already given up, and then take a lock nobody wants.
            nap = min(_POLL_SECONDS, remaining)
            if cancel is not None:
                if cancel.wait(nap):
                    os.close(fd)
                    return BoxLock()
            else:
                time.sleep(nap)
            continue
        break

    # TOTAL from here down. The flock is WON but no BoxLock owns ``fd`` yet,
    # so ANY exception in this region strands it: acquire()'s blanket handler
    # hands the caller a permissive lock (fail-open, correct for them) while the
    # flock stays held by an fd nothing can release. Every OTHER run on the box
    # is then refused — by a holder that never wrote its record, so the refusal
    # cannot even name it — for the lifetime of this process. In the gauntlet's
    # case that process is the long-lived server. That is constraint 1 inverted
    # for everyone except the caller, and invisible; it is worse than the
    # overlap the lock exists to prevent.
    #
    # ProcessLock has no equivalent hazard only because its record is
    # ``str(os.getpid())`` — digits, which cannot fail to encode. This module
    # deliberately writes argv so contention can NAME the holder, and that
    # richer record is what made the write fallible in the first place (a
    # surrogateescape byte from a non-UTF-8 filename raised UnicodeEncodeError,
    # which is not an OSError). The record write is itself hardened now, so this
    # guard is no longer load-bearing for THAT path specifically — it is here
    # because the region has two more statements, and the next one added would
    # otherwise reopen the same hole silently.
    try:
        _write_holder_record(fd)
        if export_env:
            os.environ[HELD_ENV] = str(os.getpid())
        return BoxLock(fd=fd, path=path, owns_held_env=export_env)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


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
        "Wait for it to finish:\n"
        f"  {WAIT_COMMAND}\n"
        "then re-run your pytest.\n"
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
