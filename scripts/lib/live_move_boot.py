"""The boot step's deadline, bounded runner and page keys (#3492 D2a-2).

A kit source: live_move_repair.py loads it from its own directory at boot (and
for every alert). Standard library only, never imported by path elsewhere.

Every subprocess the boot repair starts ends by one absolute deadline taken at
main()'s entry, killed if need be, and a call with no time left is never
started. After the deadline only the alert (bounded here), the report write
and the exit run, so the step ends inside the unit's TimeoutStartSec: the pin
is PRE_MAIN_S + BOOT_DEADLINE_S + ALERT_TIMEOUT_S + REPORT_S + SHELL_ALERT_S
<= TIMEOUT_START_S (5 + 75 + 3 + 1 + 4 = 88 <= 90; the shape was measured in a
transient unit with a 20 s deadline, 2026-11-11).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import time

BOOT_DEADLINE_S = 75
KILL_GRACE_S = 5  # TERM this long before the deadline, KILL at it
HOOKS_MIN_S = 5  # restore-hooks is not started with less than this left
AFTER_S = 10  # kept after the hooks step for _after: a healed tree is judged in time
ALERT_TIMEOUT_S = 3
GUARD_REAP_S = 5  # the guard's own kill-and-reap of its residue check
PRE_MAIN_S, REPORT_S, SHELL_ALERT_S = 5, 1, 4  # interpreter start; REPAIR.json; the unit's page
TIMEOUT_START_S = 90  # the units' TimeoutStartSec
_now = time.monotonic


def run(argv, *, until, grace, env=None, pass_fds=(), data=None):
    """Run ``argv`` in its own session, ending by monotonic ``until``: the
    group is sent SIGTERM at ``until - grace`` and SIGKILL at ``until``, and
    the pipes are then closed unread (a grandchild holding them cannot hold
    this up). ``until - grace`` already past: nothing is started. Raises
    subprocess.TimeoutExpired in both cases."""
    left = until - grace - _now()
    if left <= 0:
        raise subprocess.TimeoutExpired(argv[0], 0)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        pass_fds=pass_fds,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(data, timeout=left)
    except subprocess.TimeoutExpired:
        _kill(proc, grace)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _kill(proc: subprocess.Popen, grace: float) -> None:
    """TERM the group, wait up to ``grace`` for its leader, KILL the group."""
    if proc.pid <= 1:  # killpg(1) would signal every process of this user
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    end = _now() + grace
    while proc.poll() is None and _now() < end:
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()


def queue_alert(lib: str, severity: str, title: str, body: str, key: str) -> int:
    """alert_queue.sh's queue_alert from the kit dir ``lib``, bounded by
    ALERT_TIMEOUT_S (61-138 ms measured): its exit code."""
    argv = ["bash", "-c", '. "$1/alert_queue.sh" && queue_alert "$2" live_move_repair "$3" "$4" "$5"',
            "alert", lib, severity, title, body, key]  # fmt: skip
    return run(argv, until=_now() + ALERT_TIMEOUT_S, grace=0).returncode


def end_mark(git) -> str:
    """What the checkout looks like now: HEAD, `status --porcelain -uall -z`
    and whether index.lock exists, hashed. ``git(*args)`` returns stdout."""
    head = git("rev-parse", "-q", "--verify", "HEAD")
    status = git("--no-optional-locks", "status", "--porcelain", "-uall", "-z")
    lock = os.fsdecode(
        git("rev-parse", "--path-format=absolute", "--git-path", "index.lock").strip()
    )
    held = b"1" if os.path.lexists(lock) else b"0"
    return hashlib.sha256(head + b"\0" + status + b"\0" + held).hexdigest()


def key(cls: str, state: bytes, mark: str) -> str:
    """A boot page's dedupe key: its class, the state's bytes as the run leaves
    them and the checkout's end mark. The same event keys the same, so it pages
    once (the queue and outreach dedupe it); a tree that went half-written
    again under the same state pages again."""
    digest = hashlib.sha256(state + b"\0" + mark.encode()).hexdigest()[:12]
    return f"live_move_repair:boot:{cls}:{digest}"


def unresolved_repeat(
    report: dict | None, state_sha: str, mark: str, kit_id: str | None, judged: str | None = None
) -> bool:
    """Did the newest repair of these state bytes end unresolved, with the
    checkout and the kit unchanged since? Then a new heal would end the same
    way. A newly installed kit tries again: it may be the fix. A heal that
    recorded what the guard judged as it ended (``end_verdict``) repeats only
    while the guard judges ``judged`` the same: residue outside the checkout
    (through a tracked symlink) can go without changing the end mark."""
    if not isinstance(report, dict):
        return False
    result = report.get("result")
    return (
        isinstance(result, str)
        and result.startswith("unresolved")
        and report.get("state_sha") == state_sha
        and report.get("end_mark") == mark
        and report.get("kit_id") == kit_id
        and report.get("end_verdict", judged) == judged
        and not report.get("retry_left")  # a crashed heal's one retry is owed
    )
