#!/usr/bin/env python3
"""Wait until the box-wide pytest lock is free. A CLI, not a hook.

    python3 scripts/pytest_lock_wait.py            # wait, 2h ceiling
    python3 scripts/pytest_lock_wait.py --wait=600  # or an explicit bound

Exits 0 once the lock is free, 1 on timeout.

WHY THIS IS A PURE CLI. Mutual exclusion lives entirely in
``genesis.util.pytest_lock``: ``tests/conftest.py`` acquires it, and a run that
loses is refused there with a clear message and a distinct exit status, before
collection. An earlier design ALSO had a PreToolUse hook block Claude-Code Bash
pytest calls, deciding for itself whether a command opted out. That meant two
layers reading different things — one parsing a shell command, one reading its
own environment — and they drifted apart repeatedly: the hook blocked the very
overrides the lock's message prescribed, an env prefix on an unrelated segment
was read as an opt-out, an unrecognised disable value was honoured by one layer
and refused by the other, and a ``GENESIS_PYTEST_LOCK_PATH`` prefix was
invisible to the hook because the assignment had not run yet. Five defects, one
generator. Deleting the blocking layer removed the whole class; the ~2s of
pre-collection feedback it bought was not worth a second oracle.

So this program decides NOTHING about whether a run may proceed. It answers one
question — "is the lock free yet?" — and the lock itself is the only authority on
CONCURRENCY. (A separate hook, ``full_suite_guard``, still refuses whole-directory
runs; that is SCOPE, a different axis, and the two never decide the same thing.)

Blocking without a way to wait is only half a primitive, which is what this
supplies. Told merely to "wait for it to finish", callers improvise ``pgrep``
loops that match their OWN ``bash -c`` argv — the pattern they search for is
right there in their command line — so they wait on themselves forever, and
they match other sessions' waiters too. Hence a shared, correct implementation.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import sys
import time

#: The box lock. Mirrors ``genesis.util.pytest_lock``'s default; this script is
#: deliberately importable without ``genesis`` on the path (it runs from a bare
#: checkout, from cron, from any shell), so the path is duplicated and pinned by
#: a test that asserts both resolve to the same file.
LOCK_PATH = os.environ.get("GENESIS_PYTEST_LOCK_PATH") or os.path.expanduser(
    "~/.genesis/locks/pytest.lock"
)

#: Default ceiling. The project timeout-policy floor: long enough that no
#: legitimate suite is cut short, bounded so a wedged holder cannot strand a
#: caller forever.
DEFAULT_WAIT_TIMEOUT = 7200.0
#: Absolute cap — ``inf`` (via ``--wait=inf`` or a mistyped ``1e999``) must not
#: be representable as a deadline.
MAX_WAIT_TIMEOUT = 86400.0

#: Holder records are 3 short lines; cap the read so a redirected LOCK_PATH
#: cannot make this slurp an arbitrary file.
_RECORD_READ_LIMIT = 4096

_POLL_SECONDS = 3.0
_PROGRESS_SECONDS = 30.0

#: Python interpreter short options that CONSUME the next token (or the rest of
#: their own token when glued). Closed set, from `python --help`.
_PY_VALUE_OPTS = frozenset("cmWXQ")


def _python_target(argv: list[str]) -> tuple[str, str] | None:
    """What a python interpreter argv actually runs.

    ``("module", name)`` for ``-m name`` in any spelling; ``("command", "")``
    for ``-c`` or ``-`` (what follows is that command's ARGUMENTS, never a
    script to run); ``("script", path)`` for the first non-option token.
    ``None`` when the argv runs nothing identifiable.

    ONE parser, deliberately. This file previously scanned the same argv twice
    with two slightly different option models, and they disagreed in BOTH
    directions: ``python -c pass /venv/bin/pytest`` read the -c command's
    argument as a console script (false positive), while
    ``python --check-hash-based-pycs default /venv/bin/pytest`` read the long
    option's value as the script and missed a real run (false negative). Two
    oracles for one question is the same drift class that cost this change its
    second layer; one parser cannot disagree with itself.

    ``-m pytest``, ``-mpytest`` and ``-um pytest`` are the SAME invocation to
    the interpreter; a matcher that only understands the first silently misses a
    real running suite, so a holder spelled that way would go unnamed.
    """
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            return ("script", nxt) if nxt else None
        if tok == "-":
            return ("command", "")  # program on stdin
        if tok.startswith("--"):
            if tok == "--check-hash-based-pycs":  # the one long opt with a value
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            for pos, ch in enumerate(tok[1:], start=1):
                if ch not in _PY_VALUE_OPTS:
                    continue  # a boolean flag in the cluster (-u, -B, -O, …)
                rest = tok[pos + 1 :]
                value = rest if rest else (argv[i + 1] if i + 1 < len(argv) else "")
                if ch == "m":
                    return ("module", value)
                if ch == "c":
                    return ("command", "")
                i += 1 if rest else 2
                break
            else:
                i += 1  # pure boolean cluster
                continue
            continue
        return ("script", tok)  # first non-flag token: the script
    return None


def _python_module_arg(argv: list[str]) -> str | None:
    """The value of a python interpreter's ``-m``, or None. Thin over
    :func:`_python_target` so there is exactly one option model."""
    target = _python_target(argv)
    return target[1] if target and target[0] == "module" else None


def argv_is_pytest(argv: list[str]) -> bool:
    """Whether a process argv IS a pytest run (not a mere textual mention).

    Used only to NAME a holder, never to decide anything. True for argv[0]
    basenamed ``pytest``, a python interpreter resolving to ``-m pytest`` in any
    spelling, or a python running a console-script path. A ``grep pytest …``, a
    ``tail -f pytest.log``, and — the one that matters — a ``bash -c 'until !
    pgrep …'`` waiter whose own argv contains the phrase are all excluded.
    """
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base == "pytest":
        return True
    if not base.startswith("python"):
        return False
    target = _python_target(argv)
    if target is None:
        return False
    kind, value = target
    if kind == "module":
        return value == "pytest"
    if kind == "command":
        return False  # -c/-: the rest is that command's argv, not a script
    # Require a slash so a bare `python pytest` (a local file named pytest,
    # not a suite) is not misread as a run.
    return "/" in value and os.path.basename(value) == "pytest"


def scan_pytest_processes() -> list[tuple[int, str, float]]:
    """Live pytest runs as ``(pid, command, age)``, excluding our own chain.

    A NAMING aid and a stand-in where there is no lock file at all (a bare
    checkout, a CI container). Any scan error yields an empty list.
    """
    exclude = {os.getpid(), os.getppid()}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    now = time.time()
    found: list[tuple[int, str, float]] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue  # vanished or unreadable — skip
        argv = [t.decode("utf-8", "replace") for t in raw.split(b"\x00") if t]
        if not argv_is_pytest(argv):
            continue
        try:
            age = max(0.0, now - os.stat(f"/proc/{pid}").st_mtime)
        except OSError:
            age = 0.0
        found.append((pid, " ".join(argv), age))
    return found


def _format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _lock_holder_description() -> str:
    """Name the lock's holder from its record. Best-effort, never raises."""
    try:
        with open(LOCK_PATH, encoding="utf-8", errors="replace") as fh:
            lines = fh.read(_RECORD_READ_LIMIT).split("\n")
    except OSError:
        return "  (holder details unavailable)"
    pid = lines[0].strip() if lines else ""
    if not pid.isdigit():
        return "  (holder details unavailable)"
    detail = f"  pid {pid}"
    if len(lines) > 1:
        with contextlib.suppress(TypeError, ValueError):
            detail += f" (running {_format_age(time.time() - float(lines[1]))})"
    if len(lines) > 2 and lines[2].strip():
        detail += f"\n  command: {lines[2].strip()[:160]}"
    return detail


def box_lock_held() -> bool | None:
    """Is the box test lock held? ``None`` when there is nothing conclusive.

    Probe-only, the same shape as ``ProcessLock.is_locked``: briefly take the
    flock if free, then release WITHOUT unlinking (an unlink would race a holder
    that has already opened the file). The kernel drops flock on holder death,
    so a leftover file from a dead run correctly reads as free.

    ONLY ``EWOULDBLOCK``/``EAGAIN`` mean "held". Any other errno — ``EIO``,
    ``EPERM``, ``ENOLCK`` on an unhealthy or lock-less filesystem — is a failure
    to determine, not a holder, and returning True there would make this wait
    out its full timeout against a lock nobody holds. That mirrors the
    acquisition path, which already distinguishes the two.
    """
    import errno

    try:
        if not os.path.exists(LOCK_PATH):
            return None
        # O_NOFOLLOW: the lock is always a regular file; a symlink there is a
        # misconfiguration, and the resulting ELOOP is inconclusive, not held.
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return True
        return None  # not contention — inconclusive
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _describe_processes(holders: list[tuple[int, str, float]], limit: int = 3) -> str:
    lines = [
        f"  pid {pid} — {cmd[:120]} (running {_format_age(age)})"
        for pid, cmd, age in holders[:limit]
    ]
    if len(holders) > limit:
        lines.append(f"  … and {len(holders) - limit} more")
    return "\n".join(lines)


def pytest_is_active() -> tuple[bool, str]:
    """(is a governed pytest running, description of it).

    The lock decides. Only when there is no conclusive answer from it does the
    process scan stand in.
    """
    held = box_lock_held()
    if held is True:
        return True, _lock_holder_description()
    if held is False:
        return False, ""
    holders = scan_pytest_processes()
    return bool(holders), _describe_processes(holders)


def _sanitize_timeout(value: float) -> float:
    """A finite, positive, bounded wait. Never ``inf``."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_TIMEOUT
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return DEFAULT_WAIT_TIMEOUT
    return min(value, MAX_WAIT_TIMEOUT)


def parse_wait_arg(argv: list[str]) -> float | None:
    """The ``--wait`` timeout in seconds, or None when the flag is absent.

    Accepts ``--wait``, ``--wait=N`` and ``--wait N``. A malformed, non-finite
    or non-positive value falls back to the default: neither refusing to wait
    because the timeout was mistyped, nor waiting forever because it said
    ``inf``, is an acceptable outcome.
    """
    for i, tok in enumerate(argv):
        if tok == "--wait":
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            try:
                return _sanitize_timeout(float(nxt))
            except ValueError:
                return DEFAULT_WAIT_TIMEOUT
        if tok.startswith("--wait="):
            try:
                return _sanitize_timeout(float(tok.split("=", 1)[1]))
            except ValueError:
                return DEFAULT_WAIT_TIMEOUT
    return None


def wait_for_clear(timeout: float, out=None) -> int:
    """Poll until the box test lock is free. 0 when clear, 1 on timeout.

    Condition-based with a bounded deadline — never a fixed sleep. A SIGTERM
    (the shape a caller's own tool-level timeout takes) is reported as an
    explicit "still waiting" line rather than a bare exit 143.
    """
    out = out or sys.stderr
    started = time.monotonic()
    # Bound ONCE and report the bounded value: the message path used the raw
    # parameter, so an in-process caller passing inf (main() sanitises, direct
    # callers do not) crashed _format_age with OverflowError — in the one branch
    # whose job is to explain a timeout.
    bounded = _sanitize_timeout(timeout)
    deadline = started + bounded

    def _on_term(_signum, _frame):
        print(
            f"--wait interrupted after {_format_age(time.monotonic() - started)} — a "
            "pytest run is still active. Re-run it (with a larger tool timeout if "
            "this was cut short by one).",
            file=out,
        )
        out.flush()
        raise SystemExit(1)

    previous = None
    installed = False
    try:
        previous = signal.signal(signal.SIGTERM, _on_term)
        installed = True
    except ValueError:
        pass  # not the main thread — progress reporting only, not correctness

    try:
        active, detail = pytest_is_active()
        if not active:
            print("No pytest run is active — clear to go.", file=out)
            return 0
        print("Waiting for the box test lock:", file=out)
        print(detail, file=out)
        out.flush()

        next_progress = time.monotonic() + _PROGRESS_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Clamp to the deadline so a short wait reports on time rather than
            # overshooting by a whole poll interval.
            time.sleep(min(_POLL_SECONDS, remaining))
            active, detail = pytest_is_active()
            if not active:
                print(f"Clear after {_format_age(time.monotonic() - started)}.", file=out)
                return 0
            if time.monotonic() >= next_progress:
                next_progress = time.monotonic() + _PROGRESS_SECONDS
                print(
                    f"  … still waiting ({_format_age(time.monotonic() - started)})",
                    file=out,
                )
                out.flush()

        # One last look: the holder may have released between the final poll and
        # the deadline, and reporting a wedged holder when the lock is already
        # free would send the caller chasing nothing.
        active, detail = pytest_is_active()
        if not active:
            print(f"Clear after {_format_age(time.monotonic() - started)}.", file=out)
            return 0
        print(
            f"TIMEOUT: still busy after {_format_age(bounded)}.\n"
            f"{detail}\n"
            "The holder may be wedged — check it before overriding.",
            file=out,
        )
        return 1
    finally:
        # signal.signal(SIGTERM, None) raises TypeError; a handler installed
        # from C reads back as None, so only restore what we can.
        if installed and previous is not None:
            signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr)
        return 0
    timeout = parse_wait_arg(argv)
    if timeout is None:
        timeout = DEFAULT_WAIT_TIMEOUT  # bare invocation == --wait
    return wait_for_clear(timeout)


if __name__ == "__main__":
    sys.exit(main())
