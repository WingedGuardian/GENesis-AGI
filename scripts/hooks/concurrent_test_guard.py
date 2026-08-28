"""PreToolUse hook: block concurrent pytest runs — and let a caller WAIT.

Fires on every Bash tool call. If the command would run pytest AND the box-wide
test lock is already held, blocks with exit 2.

THE SIGNAL IS THE LOCK, NOT THE PROCESS TABLE. Mutual exclusion lives in
``genesis.util.pytest_lock`` (a ``flock`` on ``~/.genesis/locks/pytest.lock``,
acquired by ``tests/conftest.py`` and by the gauntlet). This hook and its
``--wait`` mode both PROBE that lock, so the layer that blocks, the layer that
waits, and the layer that actually excludes all read the same bit. An earlier
design had the hook scan ``/proc`` argv instead, which meant two oracles that
could disagree in both directions: a holder spelled ``python -um pytest`` was
invisible to the scanner, so ``--wait`` reported "clear to go" while the lock
kept refusing — an advice livelock.

The ``/proc`` scan survives for the two jobs it is genuinely good at:
  * naming a holder when the lock record is unreadable;
  * substituting for the lock entirely when there is no install (no
    ``~/.genesis``), e.g. a CI container or a bare checkout.
A pytest that is running but does NOT hold the lock is deliberately not
blocked: it either opted out (``GENESIS_PYTEST_LOCK=0``) or belongs to a
foreign project, and in both cases refusing a new run would be a false block.

``--wait`` mode
--------------
Blocking is only half a primitive: told "wait for it to finish" with no way to
wait, callers improvise ``pgrep``-based loops that match their OWN ``bash -c``
argv (the pattern is right there in the command line) and hang forever, or that
match another session's waiter and never clear. Run this script with ``--wait``
instead::

    python3 scripts/hooks/concurrent_test_guard.py --wait
    python3 scripts/hooks/concurrent_test_guard.py --wait=600

Exits 0 once the lock is free, non-zero on timeout. It must be run as its OWN
command — chaining ``--wait && pytest …`` is self-defeating, because this very
hook sees the pytest in that command and blocks the whole thing before the wait
can start.

Overrides (kept deliberately in step with what ``pytest_lock``'s own refusal
message prescribes — if the lock tells you to run something, this hook must let
you run it):
  * ``GENESIS_PYTEST_LOCK=0 pytest …``     — a deliberate concurrent run
  * ``GENESIS_PYTEST_LOCK_WAIT=1 pytest …`` — queue rather than fail
  * a trailing ``# concurrent-ok`` comment, matching ``full_suite_guard``'s idiom
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shlex
import signal
import sys
import time

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze,
    command_runs_pytest,
    has_trailing_override,
    is_pytest_invocation,
)

#: How a blocked caller is told to wait. Duplicated from
#: ``genesis.util.pytest_lock.WAIT_COMMAND`` on purpose: hooks must run without
#: importing ``genesis`` (they execute from disk, outside the venv's
#: guarantees), so the two constants are pinned equal by a test.
WAIT_COMMAND = "python3 scripts/hooks/concurrent_test_guard.py --wait"

#: The box lock. Duplicated from ``pytest_lock.default_lock_path()`` for the
#: same reason, and pinned by the same test.
LOCK_PATH = os.environ.get("GENESIS_PYTEST_LOCK_PATH") or os.path.expanduser(
    "~/.genesis/locks/pytest.lock"
)

#: Trailing-comment escape hatch, same shape as ``full_suite_guard``'s.
OVERRIDE_SIGIL = "concurrent-ok"

#: Env prefixes that opt a command out of this hook, because they opt it out of
#: (or into queueing on) the lock itself. Values mirror ``pytest_lock``'s.
_DISABLE_ENV = "GENESIS_PYTEST_LOCK"
_WAIT_ENV = "GENESIS_PYTEST_LOCK_WAIT"
_FALSEY = {"0", "off", "false", "no", "n", ""}
_TRUTHY = {"1", "on", "true", "yes", "y"}

#: Default ceiling on ``--wait``. The project timeout-policy floor.
DEFAULT_WAIT_TIMEOUT = 7200.0
#: Absolute cap — ``inf`` (via ``--wait=inf`` or a mistyped ``1e999``) must not
#: be representable as a deadline on the critical path of every test run.
MAX_WAIT_TIMEOUT = 86400.0

#: Holder records are 3 short lines; cap the read so a redirected LOCK_PATH
#: cannot make this hot path slurp an arbitrary file.
_RECORD_READ_LIMIT = 4096

_POLL_SECONDS = 3.0
_PROGRESS_SECONDS = 30.0

#: Python interpreter short options that CONSUME the next token (or the rest of
#: their own token when glued). Closed set, from `python --help`.
_PY_VALUE_OPTS = frozenset("cmWXQ")


def _command_runs_pytest(cmd: str) -> bool:
    """Whether a shell command will invoke pytest (any variant), quote-aware.

    Delegates to the canonical shell parser so a ``pytest`` mentioned only inside a
    quoted argument — e.g. ``grep 'a|pytest' f`` — is NOT treated as a run (the old
    raw-regex scan matched the ``|pytest`` inside such a grep pattern and false-blocked).
    """
    return command_runs_pytest(cmd)


def _python_module_arg(argv: list[str]) -> str | None:
    """The value of a python interpreter's ``-m``, honouring shell clustering.

    ``-m pytest``, ``-mpytest`` and ``-um pytest`` are the SAME invocation to
    the interpreter; a matcher that only understands the first silently misses a
    real running suite. (The same clustering class this repo has been bitten by
    in CLI guards before — short options cluster, and a value option takes the
    remainder of its own token when non-empty.)
    """
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return None
        if tok.startswith("--"):
            # Long options: only --check-hash-based-pycs takes a value.
            if tok == "--check-hash-based-pycs":
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
                    return value
                # -c/-W/-X/-Q consume their value; nothing after it is a flag
                # we care about on this token.
                i += 1 if rest else 2
                break
            else:
                i += 1  # pure boolean cluster
                continue
            continue
        return None  # first non-flag token: the script, not a -m module
    return None


def _argv_is_pytest(argv: list[str]) -> bool:
    """Whether a process argv IS a pytest run (not a mere textual mention).

    True only for:
      * argv[0] whose basename is exactly ``pytest`` (venv/system entrypoint);
      * a python interpreter (basename starts with ``python``) whose options
        resolve to ``-m pytest`` in ANY spelling (``-m pytest``, ``-mpytest``,
        ``-um pytest``);
      * a python interpreter running a console-script path (``python
        /venv/bin/pytest``), which is this repo's normal invocation.
    Everything else — ``grep pytest …``, ``tail -f pytest.log``, an editor on
    ``pytest.ini``, and crucially a ``bash -c 'until ! pgrep …'`` WAITER whose
    own argv contains the phrase — is NOT a pytest process. Pure function;
    hermetically testable.
    """
    if not argv:
        return False
    base = os.path.basename(argv[0])
    if base == "pytest":
        return True
    if not base.startswith("python"):
        return False
    if _python_module_arg(argv) == "pytest":
        return True
    # Console-script form: the first non-flag argument is the script to run.
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("-") and len(tok) > 1:
            consumed = False
            for pos, ch in enumerate(tok[1:], start=1):
                if ch in _PY_VALUE_OPTS:
                    i += 1 if tok[pos + 1 :] else 2
                    consumed = True
                    break
            if not consumed:
                i += 1
            continue
        # Require a slash so a bare `python pytest` (a local file named pytest,
        # not a suite) is not misread as a run.
        return "/" in tok and os.path.basename(tok) == "pytest"
    return False


def scan_pytest_processes() -> list[tuple[int, str, float]]:
    """Live pytest runs as ``(pid, command, age_seconds)``, excluding our own chain.

    The FALLBACK signal — see the module docstring. Reads each
    /proc/<pid>/cmdline (NUL-separated argv, one syscall per process, the same
    approach as worktree_cwd_guard's cwd scan). Processes that vanish mid-scan
    are skipped, and any scan error yields an empty list: this guard is a
    resource-contention convenience, not an irreversible-action gate.

    Age comes from the mtime of ``/proc/<pid>``, which tracks process start
    within a few seconds — deliberately preferred over reconstructing it from
    ``/proc/stat`` btime plus stat field 22, which is fiddly, easy to misindex,
    and buys precision no human-readable "running 4m" needs.
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
            continue  # process vanished or unreadable — skip
        argv = [t.decode("utf-8", "replace") for t in raw.split(b"\x00") if t]
        if not _argv_is_pytest(argv):
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
            # Bounded: only three short lines are ever needed, and LOCK_PATH is
            # env-redirectable, so an unbounded read on a per-Bash-call hot path
            # could be pointed at an arbitrarily large file.
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
    """Is the box test lock held? ``None`` when there is no lock to consult.

    Probe-only, and the same shape as ``ProcessLock.is_locked``: briefly take
    the flock if free, then release WITHOUT unlinking (an unlink would race a
    holder that has already opened the file). flock is dropped by the kernel on
    holder death, so a leftover file from a dead run correctly reads as free.

    Returns ``None`` — meaning "fall back to the process scan" — when there is
    no install, no lock file yet, or the file cannot be opened. Never raises.
    """
    try:
        if not os.path.exists(LOCK_PATH):
            return None
        # O_NOFOLLOW: the lock is always a regular file we create; a symlink
        # there is a misconfiguration, and ELOOP lands on the fail-open path.
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True  # held by someone else
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

    The lock decides. Only when there is no lock to consult does the process
    scan stand in for it.
    """
    held = box_lock_held()
    if held is not None:
        return held, (_lock_holder_description() if held else "")
    holders = scan_pytest_processes()
    return bool(holders), _describe_processes(holders)


def _is_env_assignment(tok: str) -> bool:
    """Leading ``VAR=value`` shell assignment — the same closed test shell_parse
    uses when it strips them out of ``Segment.argv``."""
    return "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier()


def command_opts_out(cmd: str) -> bool:
    """Whether the command opts out of the lock, and so out of this hook.

    The refusal message printed by ``pytest_lock`` prescribes
    ``GENESIS_PYTEST_LOCK=0`` and ``GENESIS_PYTEST_LOCK_WAIT=1``. If this hook
    blocked those, the documented recovery path would be unreachable from a
    Bash call — a wedged holder would mean nobody can run a test at all. So the
    hook honours exactly the levers the lock advertises, plus a trailing
    ``# concurrent-ok`` sigil.

    The check is SCOPED TO THE PYTEST SEGMENT, because that is what bash does.
    An env prefix binds to one simple command, so in
    ``GENESIS_PYTEST_LOCK=0 true; pytest tests/`` the variable reaches ``true``
    and NOT ``pytest`` — measured against real bash, which reports zero
    occurrences of it in pytest's environment. An earlier version scanned every
    segment and waved that command through, silently disabling the guard for a
    pytest that never actually opted out. Segment selection reuses
    ``shell_parse.is_pytest_invocation`` — the same predicate
    ``command_runs_pytest`` is built from, so the two cannot disagree about
    which segment is the run.
    """
    try:
        segments = [s for s in analyze(cmd) if is_pytest_invocation(s)]
    except Exception:  # noqa: BLE001 — parse failure must not deny an override
        return False
    for seg in segments:
        if has_trailing_override(seg.raw, OVERRIDE_SIGIL):
            return True
        # Env assignments are stripped from Segment.argv, so read the raw text.
        # shlex is quote-aware, so a '#' inside a quoted VALUE stays part of
        # that value; a real trailing comment tokenizes as a bare '#', which is
        # not an assignment and simply ends the prefix scan. (Splitting on the
        # first literal '#' instead — as this once did — truncated mid-quote,
        # raised, and swallowed a genuine opt-out, blocking a run the caller
        # had correctly disabled the lock for.)
        try:
            tokens = shlex.split(seg.raw)
        except ValueError:
            continue
        for tok in tokens:
            if not _is_env_assignment(tok):
                break  # past the assignment prefix — this is the command word
            name, _, value = tok.partition("=")
            token = value.strip().lower()
            if name == _DISABLE_ENV and token in _FALSEY:
                return True
            if name == _WAIT_ENV and token in _TRUTHY:
                return True
    return False


def _sanitize_timeout(value: float) -> float:
    """A finite, positive, bounded wait. Never ``inf``."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WAIT_TIMEOUT
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return DEFAULT_WAIT_TIMEOUT
    return min(value, MAX_WAIT_TIMEOUT)


def _parse_wait_arg(argv: list[str]) -> float | None:
    """The ``--wait`` timeout in seconds, or None when not in wait mode.

    Accepts ``--wait``, ``--wait=N`` and ``--wait N``. A malformed, non-finite
    or non-positive value falls back to the default rather than erroring: this
    is a convenience primitive, and neither refusing to wait because the
    timeout was mistyped nor waiting FOREVER because it said ``inf`` is an
    acceptable outcome.
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
    explicit "still waiting" line rather than a bare exit 143, so a truncated
    wait is legible instead of mysterious.
    """
    out = out or sys.stderr
    started = time.monotonic()
    deadline = started + _sanitize_timeout(timeout)

    def _on_term(_signum, _frame):
        elapsed = time.monotonic() - started
        print(
            f"--wait interrupted after {_format_age(elapsed)} — a pytest run is "
            "still active. Re-run --wait (with a larger tool timeout if this was "
            "cut short by one).",
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
            # Clamp to the deadline so a short --wait reports on time rather
            # than overshooting by a whole poll interval.
            time.sleep(min(_POLL_SECONDS, remaining))
            active, detail = pytest_is_active()
            if not active:
                print(
                    f"Clear after {_format_age(time.monotonic() - started)}.",
                    file=out,
                )
                return 0
            if time.monotonic() >= next_progress:
                next_progress = time.monotonic() + _PROGRESS_SECONDS
                print(
                    f"  … still waiting ({_format_age(time.monotonic() - started)})",
                    file=out,
                )
                out.flush()

        _active, detail = pytest_is_active()
        print(
            f"TIMEOUT: still busy after {_format_age(timeout)}.\n"
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


def _block_message(detail: str) -> str:
    return (
        "BLOCKED: a pytest run holds the box-wide test lock.\n"
        f"{detail or '  (holder details unavailable)'}\n"
        "Concurrent suites contend for the CPU and RAM of the live Genesis "
        "services on a swapless box and take far longer than running in turn.\n"
        "Wait for it — run this as its OWN command, NOT chained before your "
        "pytest with '&&' (this hook would see the pytest in that same command "
        "and block the whole thing, so the wait would never start):\n"
        f"  {WAIT_COMMAND}\n"
        "then re-run your pytest.\n"
        f"Deliberate concurrent run: {_DISABLE_ENV}=0 pytest ...\n"
        f"Queue instead of failing: {_WAIT_ENV}=1 pytest ...\n"
        f"Anything else: append '# {OVERRIDE_SIGIL}' to the command."
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Parse the CLI mode BEFORE touching stdin: --wait is invoked from a shell
    # with no hook payload, and read_payload() would block waiting for one.
    timeout = _parse_wait_arg(argv)
    if timeout is not None:
        return wait_for_clear(timeout)

    cmd = field(read_payload(), "command")
    if not cmd:
        return 0

    if not _command_runs_pytest(cmd):
        return 0

    # Honour the same levers the lock's own refusal message prescribes.
    if command_opts_out(cmd):
        return 0

    active, detail = pytest_is_active()
    if active:
        print(_block_message(detail), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
