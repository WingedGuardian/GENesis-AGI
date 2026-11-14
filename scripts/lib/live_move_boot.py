"""The boot step's deadline, bounded runner and page keys (#3492 D2a-2).

A kit source: live_move_repair.py loads it from its own directory at boot (and
for every alert). Standard library only, never imported by path elsewhere.

Every subprocess the boot repair starts ends by one absolute deadline taken at
main()'s entry, killed if need be, and a call with no time left is never
started. After the deadline only the alert (bounded here), the report write
and the exit run, so the step ends inside the unit's TimeoutStartSec: the pin
is PRE_MAIN_S + BOOT_DEADLINE_S + ALERT_TIMEOUT_S + REPORT_S + SHELL_ALERT_S
<= TIMEOUT_START_S (5 + 75 + 3 + 1 + 4 = 88 <= 90; the shape was measured in a
transient unit with a 20 s deadline, 2026-11-11). The heal's own git work ends
END_MARK_S before BOOT_DEADLINE_S: the end marks taken after it (a crash's, the
page key's) run in that reserve, so they are real marks and the sum is unchanged
(#4548; an allowance after the deadline would start nothing).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
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
END_MARK_S = 5  # the end of BOOT_DEADLINE_S kept for end marks (3 git calls, ~60 ms each)
MARK_GRACE_S = 1  # a mark's git is TERMed this long before BOOT_DEADLINE_S
# lstat identities a mark holds: 5,750 took 72-102 ms here, so ~0.35 s (extrapolated).
MARK_PATHS = 20000
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


def end_mark(git, root: str | None = None, skip: str | None = None) -> str:
    """What the checkout looks like now: HEAD, `status --porcelain -uall -z`
    and whether index.lock exists, hashed. ``git(*args)`` returns stdout.

    With ``root``, also the lstat identity (mode, size, mtime, ctime, inode) of
    each path the status lists, so an edit to a file that is already modified
    or untracked moves the mark too (#4548; porcelain alone does not change).
    ctime is kept because no tool sets it back, unlike mtime. Paths matching
    ``skip`` (EPHEMERAL_DIRTY_RE, matched whole against " " + path as the guard
    does) are left out: tools rewrite them on their own. Past MARK_PATHS paths
    the rest are not stat'ed, so an edit beyond them alone is not seen: that
    refuses (fails closed), as a mark with no stat part did."""
    head = git("rev-parse", "-q", "--verify", "HEAD")
    status = git("--no-optional-locks", "status", "--porcelain", "-uall", "-z")
    lock = os.fsdecode(
        git("rev-parse", "--path-format=absolute", "--git-path", "index.lock").strip()
    )
    held = b"1" if os.path.lexists(lock) else b"0"
    parts = [head, status, held]
    if root is not None:
        parts.append(_identities(root, status, re.compile(f"(?:{skip})") if skip else None))
    return hashlib.sha256(b"\0".join(parts)).hexdigest()


def _identities(root: str, status: bytes, skip) -> bytes:
    """lstat identity of each path a `status --porcelain -z` lists (a rename's
    or copy's source field skipped); "gone" when lstat fails."""
    fields, out, i = status.split(b"\0"), [], 0
    while i < len(fields) and len(out) < MARK_PATHS:
        field, i = fields[i], i + 1
        if len(field) < 4:
            continue
        if b"R" in field[:2] or b"C" in field[:2]:
            i += 1  # the source path follows as its own field
        path = os.fsdecode(field[3:])
        if skip is not None and skip.fullmatch(" " + path):
            continue
        try:
            st = os.lstat(os.path.join(root, path))
        except OSError:
            ident = b"gone"
        else:
            ident = (
                f"{st.st_mode} {st.st_size} {st.st_mtime_ns} {st.st_ctime_ns} {st.st_ino}".encode()
            )
        out.append(field[3:] + b"\0" + ident)
    return b"\0".join(out)


def key(cls: str, state: bytes, mark: str) -> str:
    """A boot page's dedupe key: its class, the state's bytes as the run leaves
    them and the checkout's end mark. The same event keys the same, so it pages
    once (the queue and outreach dedupe it); a tree that went half-written
    again under the same state pages again."""
    digest = hashlib.sha256(state + b"\0" + mark.encode()).hexdigest()[:12]
    return f"live_move_repair:boot:{cls}:{digest}"


CRASHED = "unresolved: the boot repair failed partway"  # _record_unresolved's result


def changed(stored: object, now: object) -> bool:
    """Did the checkout change between two end marks? Only provable when both
    are real marks: one the deadline left "unknown" (or never written) proves
    nothing, so it never counts as a change (#4501 round 3)."""
    return _real_mark(stored) and _real_mark(now) and stored != now


def _real_mark(mark: object) -> bool:
    return isinstance(mark, str) and len(mark) == 64 and mark != "unknown"


def same_attempt(report: dict, state_sha: str, kit_id: str | None) -> bool:
    """A repair of these state bytes by this kit."""
    return report.get("state_sha") == state_sha and report.get("kit_id") == kit_id


def crashed(report: dict) -> bool:
    """A heal that crashed partway, or one killed before it recorded a result
    (None, or "superseded", which the next heal's _save writes over None)."""
    result = report.get("result")
    return result in (None, "superseded") or (
        isinstance(result, str) and result.startswith(CRASHED)
    )


def unresolved_repeat(
    report: dict | None, state_sha: str, mark: str, kit_id: str | None, judged: str | None = None
) -> bool:
    """Did the newest repair of these state bytes end unresolved, with the
    checkout and the kit unchanged since? Then a new heal would end the same
    way. A newly installed kit tries again: it may be the fix. A heal that
    recorded what the guard judged as it ended (``end_verdict``) repeats only
    while the guard judges ``judged`` the same: residue outside the checkout
    (through a tracked symlink) can go without changing the end mark. An end
    mark nobody could read is not proof of a change."""
    if not isinstance(report, dict):
        return False
    result = report.get("result")
    return (
        isinstance(result, str)
        and result.startswith("unresolved")
        and same_attempt(report, state_sha, kit_id)
        and not changed(report.get("end_mark"), mark)
        and report.get("end_verdict", judged) == judged
    )
