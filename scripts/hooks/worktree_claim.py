#!/usr/bin/env python3
"""Worktree ownership: record who holds a worktree, as a fact rather than a guess.

Many Claude Code sessions share one repository and 200+ worktrees, and nothing
records which session is working in which. The reaper tries to infer it, and the
signal it infers from does not exist here.

WHAT THIS MODULE IS FOR, precisely: a session can be working in a worktree while
every signal the reaper has says nobody is. This records the one fact that
settles it — which live process is using this worktree — in a form the reaper
already honours.

THE TWO SIGNALS IT REPLACES, both MEASURED DEAD rather than assumed:

  * ``/proc/*/cwd`` ownership. ``worktree_cwd_guard.py`` and
    ``worktree_lifecycle.py`` each scan for a process sitting inside the
    worktree. 2026-09-10: 0 of 200 worktrees had any process CWD inside them
    while 7 session processes were running. Sessions ``cd`` per command, so a
    session's process CWD never leaves the main checkout.
  * mtime staleness. ``_last_activity_time`` samples the worktree root and TWO
    directory levels, and modifying a file updates that file's mtime only, never
    its ancestors'. 2026-09-11, with a control that moves: a worktree backdated
    19 days reported 19.0d; editing ``src/genesis/memory/store.py`` left it at
    19.0d and still reapable; editing ``README.md`` (depth 1) correctly reported
    0.0d. git saw the deep edit (``M src/genesis/memory/store.py``); the walk did
    not. Nearly all source in this repo lives below the sampled depth, so this is
    the common case.

WHY A ``git worktree lock`` AND NOT A CLAIM FILE. Two enforcement points already
honour it with no code to add to either: the reaper classifies a locked worktree
as protected, and ``git worktree remove`` refuses one. MEASURED with the control
moving in both directions.

WHAT THIS DELIBERATELY DOES NOT RECORD. Dirtiness. The reaper can compute that
itself, at the instant it decides, and does. Recording it here would mean
sampling it in one process and acting on it in another, and everything that
followed from that arrangement in an earlier design — a return code so one step
could tell the next, a re-check because the value went stale, a reconciliation
with a third tool — was a seam that generated its own defects. **A lock records
what cannot be derived; everything derivable is derived at the point of use.**
Only the claiming session knows its own pid, so that is the only thing here.

THE REASON LEADS WITH A SENTENCE, AND THAT IS NOT COSMETIC. git echoes the lock
reason verbatim when it refuses a removal, so the reason is read by a human or an
agent at exactly the moment they are blocked. A bare JSON blob there explains
nothing. The machine-readable half follows and is parsed from the first ``{``.

A lock whose reason is NOT our namespaced JSON is FOREIGN — a human's
``git worktree lock --reason "do not touch"``, or another tool's. Never parsed
for meaning, never auto-released. Refusing to interpret someone else's lock is
the whole point of distinguishing them.

Stdlib only, and deliberately so: this is imported by a PreToolUse hook on the
latency path and read by a reaper that may run under the system interpreter when
the venv is absent. ``yaml`` is imported lazily and only for the config read,
which falls back to defaults when it is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------
# Lock payload
# --------------------------------------------------------------------------

PAYLOAD_VERSION = 1

# The namespace that makes a payload OURS. Without it, any lock reason ending in
# generic JSON -- `manual hold {"v":1,"rule":"claim"}` -- parses as ours and
# becomes eligible for auto-release, which breaks the one invariant this module
# is built on: a lock we did not write is never touched. `v` and `rule` are
# common enough words to be written by accident; this is not.
PAYLOAD_NAMESPACE = "genesis.worktree-ownership"

# One rule, because one fact needs recording. `rule` survives as a field so a
# future kind of ownership can be added without every existing lock becoming
# unreadable -- and so an unrecognised rule reads as FOREIGN rather than as
# something to act on.
RULE_CLAIM = "claim"
RULES = (RULE_CLAIM,)

# A session process: argv[0]'s basename is exactly `claude`. MEASURED: the
# wrapper that launches it (`bash -c cd ... && claude ...`) also contains the
# string "claude", so a substring test over the whole cmdline matches the shell
# too and would record the WRONG pid -- one that outlives the session it wraps.
_SESSION_EXE = "claude"

_GIT_TIMEOUT = 30

# Session ids are UUID-shaped. Anything else is not one, and is refused rather
# than escaped -- the id is echoed into a lock reason a human reads.
_SID_RE = re.compile(r"[0-9a-fA-F-]{1,64}\Z")

# Locks written by something else that we can nonetheless NAME. Claude Code's own
# `isolation: "worktree"` subagents lock the worktree they create, with a reason
# like:
#
#     claude agent agent-<id> (pid 12345 start 1362340)
#
# which is the same pid+starttime identity this module settled on independently.
# Recognising it buys a useful report and nothing else -- these stay FOREIGN and
# are never auto-released. MEASURED 2026-09-10: of 6 agent worktrees on a live
# install, only the one with a running agent was still locked, so the harness
# does clean up after itself and there is no leak to chase.
_THIRD_PARTY = ((re.compile(r"^claude agent\b"), "claude agent"),)


def describe_foreign(raw: str) -> str:
    """A short label for a lock we did not write, for reporting only."""
    for pattern, label in _THIRD_PARTY:
        if pattern.search(raw):
            return label
    return "foreign"


class Lock:
    """A parsed ``locked`` file.

    ``payload`` is our JSON when this lock is ours, else None. ``foreign`` is
    True for a lock we did not write -- never released, never interpreted.
    """

    __slots__ = ("raw", "payload", "foreign")

    def __init__(self, raw: str, payload: dict | None, foreign: bool) -> None:
        self.raw = raw
        self.payload = payload
        self.foreign = foreign

    @property
    def rule(self) -> str | None:
        return self.payload.get("rule") if self.payload else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Lock(rule={self.rule!r}, foreign={self.foreign})"


# --------------------------------------------------------------------------
# Worktree geometry
# --------------------------------------------------------------------------


def worktree_root_for(path: str | Path) -> Path | None:
    """The worktree containing ``path``, or None if it is not in a linked worktree.

    A linked worktree's ``.git`` is a FILE holding a ``gitdir:`` pointer; the main
    checkout's is a directory. So this returns None for the main tree by
    construction, which is correct -- the main tree is never claimed or reaped.

    Resolves symlinks first so two spellings of one worktree cannot be treated as
    two different worktrees.
    """
    try:
        current = Path(path).resolve()
    except OSError:
        return None
    if current.is_file() or not current.exists():
        current = current.parent
    for candidate in (current, *current.parents):
        dot_git = candidate / ".git"
        try:
            if dot_git.is_file():
                return candidate
        except OSError:
            continue
        if candidate == candidate.parent:
            break
    return None


def gitdir_for(root: Path) -> Path | None:
    """The admin directory a worktree's ``.git`` file points at.

    This is where git keeps the ``locked`` file, so it is the only place the lock
    reason can be read in its RAW form. Do not read the reason out of
    ``git worktree list --porcelain`` instead: porcelain JSON-escapes it (a lock
    reason containing ``"`` comes back as ``\\"``), so a parser fed porcelain sees
    different bytes than were written.
    """
    try:
        text = (root / ".git").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            target = line[len("gitdir:") :].strip()
            if not target:
                return None
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = (root / candidate).resolve()
            return candidate
    return None


# --------------------------------------------------------------------------
# Process liveness
# --------------------------------------------------------------------------


def _proc_fields(pid: int) -> list[str] | None:
    """Fields of ``/proc/<pid>/stat`` AFTER comm, or None if unreadable.

    comm (field 2) is the executable name in parentheses and may itself contain
    spaces and parentheses, so the file cannot be split on whitespace from the
    left. Everything after the LAST ``)`` can be, which is the documented way to
    read this file.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    return raw[close + 2 :].split()


def proc_starttime(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat`` -- when this process began, in clock ticks.

    This is the defence against PID reuse. A pid alone cannot say whether it is
    still the process that took the claim: pids recycle, and a long-lived claim
    outliving its session could be matched against an unrelated new process. The
    start time pins the identity, because a recycled pid necessarily has a later
    one.
    """
    fields = _proc_fields(pid)
    if fields is None or len(fields) <= 19:
        return None
    try:
        # index 0 is state (field 3), so field 22 is index 19.
        return int(fields[19])
    except ValueError:
        return None


def proc_ppid(pid: int) -> int | None:
    """Field 4 of ``/proc/<pid>/stat`` -- the parent pid."""
    fields = _proc_fields(pid)
    if fields is None or len(fields) <= 1:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def is_session_process(pid: int) -> bool:
    """True when ``pid`` is a Claude Code session process.

    Tests the BASENAME of argv[0], not the whole command line: the launcher shell
    (``bash -c cd <repo> && claude ...``) contains "claude" in its cmdline too,
    and recording the launcher's pid would produce a claim that stays "live"
    after the session inside it has exited.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            argv = handle.read().split(b"\x00")
    except OSError:
        return False
    if not argv or not argv[0]:
        return False
    return os.path.basename(argv[0].decode(errors="replace")) == _SESSION_EXE


def session_pid_from_ancestry(start_pid: int | None = None, max_hops: int = 12) -> int | None:
    """Walk up the parent chain to the session process that spawned this one.

    A hook receives its session's id on stdin but NOT its pid, and the obvious
    database route is unavailable: MEASURED 2026-09-10, ``cc_sessions`` reported
    1 row as active while 7 session processes were running, and carried a NULL
    pid on every one of them. Process ancestry is the signal that does exist --
    a hook is a descendant of the session that fired it, measured at 2 hops.

    The walk is bounded and tolerant of extra layers because the number of hops
    is not a stable contract: a hook may be spawned through an intermediate
    shell, and the launcher may or may not ``exec``. Returns None rather than
    guessing when no session process is found, and every caller treats None as
    "no claim can be made" instead of substituting a different pid.
    """
    current = os.getpid() if start_pid is None else start_pid
    for _ in range(max_hops):
        if current <= 1:
            return None
        if is_session_process(current):
            return current
        parent = proc_ppid(current)
        if parent is None or parent == current:
            return None
        current = parent
    return None


def pid_is_live_session(pid: int | None, start: int | None = None) -> bool:
    """True when ``pid`` is still the live session process the claim was taken by.

    ``start`` is the start time recorded at claim time. When it is supplied it
    must match, which is what makes a recycled pid read as dead rather than
    alive. When it is absent -- a lock written by an older version -- the check
    degrades to "is a session process", which is weaker but still correct for the
    common case of a session that simply exited.
    """
    if not pid or pid <= 1:
        return False
    if not is_session_process(pid):
        return False
    if start is None:
        return True
    return proc_starttime(pid) == start


# --------------------------------------------------------------------------
# Reading and writing locks
# --------------------------------------------------------------------------


def _parse_payload(raw: str) -> dict | None:
    """Our JSON out of a lock reason, or None when this is not our lock.

    Parses from the first ``{`` to the end. The leading sentence we write never
    contains a brace, and a reason that fails any check is treated as foreign
    rather than repaired -- misreading someone else's lock as ours is the one
    error that would auto-release work we do not own.
    """
    start = raw.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(raw[start:])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    # The namespace comes FIRST and is the load-bearing check. Version and rule
    # alone are words a human could plausibly write in a hand-made lock reason,
    # and treating such a lock as ours would auto-release it and expose the work
    # it was protecting.
    if payload.get("ns") != PAYLOAD_NAMESPACE:
        return None
    if payload.get("v") != PAYLOAD_VERSION:
        return None
    if payload.get("rule") not in RULES:
        return None
    # Validate the WHOLE payload, not just the discriminators. A claim missing
    # its pid, or carrying a non-integer one, has no usable release condition --
    # `pid_is_live_session` would read it as dead and release immediately, which
    # is the opposite of what a malformed claim should do.
    if not isinstance(payload.get("pid"), int) or payload["pid"] <= 1:
        return None
    start_time = payload.get("start")
    if start_time is not None and not isinstance(start_time, int):
        return None
    return payload


def read_lock(root: Path) -> Lock | None:
    """The lock on ``root``, or None when it is not locked."""
    gitdir = gitdir_for(root)
    if gitdir is None:
        return None
    try:
        raw = (gitdir / "locked").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    payload = _parse_payload(raw)
    return Lock(raw=raw, payload=payload, foreign=payload is None)


def format_reason(payload: dict) -> str:
    """A lock reason: a sentence a blocked reader can act on, then the JSON.

    git prints this verbatim in "cannot remove a locked working tree", so the
    sentence has to say who holds it and what would make it safe to release.
    """
    pid = payload.get("pid")
    lead = (
        f"Claimed by a live Claude Code session (pid {pid}). "
        "If that process is gone this lock is stale and safe to release."
    )
    return f"{lead} {json.dumps(payload, separators=(',', ':'), sort_keys=True)}"


def build_payload(
    rule: str = RULE_CLAIM, *, sid: str | None = None, pid: int | None = None
) -> dict | None:
    """The JSON half of a lock reason, or None when the claim cannot be satisfied.

    A claim without a resolvable session pid returns None rather than a lock with
    no release condition. That is the whole reason blanket-locking was rejected:
    a lock nothing can decide to remove is indistinguishable from a leak, and 200
    of them would stop the reaper permanently.
    """
    if rule not in RULES:
        return None
    if pid is None:
        pid = session_pid_from_ancestry()
    if pid is None:
        return None
    start = proc_starttime(pid)
    if start is None:
        return None
    payload = {
        "ns": PAYLOAD_NAMESPACE,
        "v": PAYLOAD_VERSION,
        "rule": rule,
        "pid": pid,
        "start": start,
    }
    if sid and _SID_RE.match(sid):
        payload["sid"] = sid
    return payload


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def lock_worktree(root: Path, payload: dict) -> bool:
    """Lock ``root`` with ``payload``. Returns False if it was already locked.

    Never overwrites an existing lock. An existing lock is another rule's or
    another session's statement of ownership, and replacing it would discard the
    release condition that makes it removable. git refuses a second lock anyway;
    checking first is what keeps the answer deterministic and off the /proc walk
    that ``build_payload`` would otherwise perform.
    """
    if read_lock(root) is not None:
        return False
    result = _git(root, "worktree", "lock", "--reason", format_reason(payload), str(root))
    return bool(result and result.returncode == 0)


def unlock_worktree(root: Path) -> bool:
    """Release our lock on ``root``. Refuses to touch a FOREIGN lock."""
    lock = read_lock(root)
    if lock is None:
        return False
    if lock.foreign:
        return False
    result = _git(root, "worktree", "unlock", str(root))
    return bool(result and result.returncode == 0)


def is_releasable(lock: Lock) -> tuple[bool, str]:
    """Whether ``lock`` may be released now, and why.

    Returns ``(releasable, reason)``; the reason is reported either way, so a
    lock that stays explains itself as loudly as one that goes.

    ONE condition, deliberately. An earlier design also released a live
    session's claim once the worktree looked idle, to stop a long-running session
    pinning a worktree for its whole life. That was built on the mtime staleness
    signal, which is MEASURED unsound for any file deeper than two directory
    levels — so it would have released claims on worktrees being actively edited.
    It is gone. A claim ends when its process ends, and a claim held by a live
    session is correct: that session may well come back to it, and the only cost
    is a merged worktree not being reclaimed while its owner is still running.

    A FOREIGN lock is never releasable, and neither is a lock whose rule we do not
    recognise — both mean "someone else's statement", including a statement from a
    future version of this module.
    """
    if lock.foreign or lock.payload is None:
        return False, f"{describe_foreign(lock.raw)} lock (not ours) — left untouched"
    if lock.rule != RULE_CLAIM:
        return False, f"unrecognised rule {lock.rule!r} — left untouched"
    pid = lock.payload.get("pid")
    if not pid_is_live_session(pid, lock.payload.get("start")):
        return True, f"claiming session (pid {pid}) is gone"
    return False, f"claimed by live session pid {pid}"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

MODES = ("off", "advisory")

_CONFIG_NAME = "worktree_ownership.yaml"
_ENV_KILL_SWITCH = "GENESIS_WORKTREE_OWNERSHIP"

DEFAULTS: dict = {"enabled": True, "mode": "advisory"}


def _config_path() -> Path:
    # scripts/hooks/worktree_claim.py → repo root is ../../
    return Path(__file__).resolve().parent.parent.parent / "config" / _CONFIG_NAME


def _overlay_path() -> Path:
    """The ``.local.yaml`` overlay, resolved USER-DIR-FIRST.

    This precedence is not a preference, it is a correctness requirement, and
    getting it wrong makes the whole lever silently inert: the settings API
    writes overrides to ``~/.genesis/config/<stem>.local.yaml`` (deliberately, so
    user config never lands in a PR), while an earlier version of this loader
    looked only at the repository's ``config/`` directory. The result was that
    ``settings_update("worktree_ownership", {"enabled": false})`` would report
    success, ``settings_get`` would display the override, and the mechanism would
    go on using the default -- a lever that appears to work and does nothing.

    Mirrors ``genesis._config_overlay._resolve_overlay_path``, which cannot be
    imported here: this module has to run under an interpreter with no
    ``genesis`` package on its path. Any change to the precedence there belongs
    here too, and ``test_the_overlay_precedence_matches_the_canonical_resolver``
    fails if the two disagree.
    """
    local_name = _config_path().with_suffix(".local.yaml").name
    user_path = Path.home() / ".genesis" / "config" / local_name
    if user_path.is_file():
        return user_path
    return _config_path().with_suffix(".local.yaml")


def load_config() -> dict:
    """The merged config, read fresh per call. Never raises.

    Mirrors ``genesis.observability.mcp_staleness_guard_config`` in shape, but
    reads the file directly instead of importing it: this module is imported by a
    hook that may run under an interpreter with no ``genesis`` package on its
    path. A missing ``yaml`` degrades to defaults rather than failing, which keeps
    the mechanism working in exactly the degraded environments where a silent
    import error would otherwise disable it invisibly.
    """
    merged = dict(DEFAULTS)
    try:
        import yaml
    except ImportError:
        return merged
    for path in (_config_path(), _overlay_path()):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if isinstance(loaded, dict):
            merged.update(loaded)
    return merged


def effective_mode() -> str:
    """The mode this mechanism runs under, read live.

    Degrades an invalid value to ``advisory`` rather than ``off``. Every surface
    here is non-blocking -- a lock the reaper already honours, and a hook that
    writes to stderr and exits 0 -- so the safest failure is to keep protecting,
    not to stop.
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # An unquoted `mode: off` parses as a YAML 1.1 boolean.
        return "off"
    if mode not in MODES:
        return "advisory"
    return mode
