"""Is an interactive `claude` actually running inside a tmux slot?

`cc-slot.sh` attaches to an existing `cc-N` session with `tmux new-session -A`.
When the session already exists, tmux ATTACHES and **silently discards the
shell-command argument** — so the launch command never runs. A slot that is
alive but sitting at a bare shell prompt is therefore self-perpetuating: every
subsequent connection lands at that prompt, and the door has no way to notice.

This module answers the one question the door needs before it decides whether
to attach or to relaunch.

WHY NOT ``#{pane_current_command}``
-----------------------------------
It is the obvious signal and it is WRONG here, which is worth recording because
it reads as correct. MEASURED on a live install, all five sessions:

    session  pane command shape                     pane_current_command  claude?
    cc-4     bash -c "cd … && claude …; trailer"    bash                  YES
    cc-5     bash -c "cd … && claude …; trailer"    bash                  YES
    cc-6     login shell, claude typed by hand      claude                YES
    cc-7     legacy `exec claude`                   claude                YES
    lobby    login shell, idle                      bash                  NO

The canonical pane command is a NON-interactive `bash -c`, and it deliberately
dropped `exec` so the exit-capture trailer can run after claude returns. Without
job control, claude shares the shell's process group, so tmux resolves the tty's
foreground group to the group LEADER — the shell. The two sessions that report
`claude` do so only because they are the legacy/manual shapes. In other words
the signal is wrong for exactly the sessions the launcher creates, and reading
it would classify a healthy slot as broken.

FAIL DIRECTION
--------------
Deliberately biased toward reporting ALIVE. A false ALIVE costs a plain attach —
which is the pre-existing behaviour, so nothing is lost. A false POISONED makes
the door type a launch command into a pane where a session IS running, i.e. into
a live TUI. The two errors are not symmetric, and every ambiguity resolves the
cheap way.

That is also why this does not reuse ``observability/cc_slots._is_interactive``:
that predicate treats an unreadable cmdline as NOT interactive, which is correct
for its purpose (never let an internal `claude -p` masquerade as a slot) and
exactly backwards for this one.

Reads only `comm` and `cmdline` under `/proc` — both world-readable for the
same uid. Never `environ`, which is ptrace-gated and returns EACCES under a
hardened service sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

ALIVE = "ALIVE"
POISONED = "POISONED"
UNKNOWN = "UNKNOWN"

# Idleness verdicts (`--idle`): "is it SAFE TO TYPE here", the second question
# the door asks after liveness says POISONED. Only IDLE permits keystrokes.
IDLE = "IDLE"
BUSY = "BUSY"

# A pane shell -> claude is one hop; a hand-typed `bash` in between makes two.
# The bound only stops a cycle in a malformed /proc from spinning.
_MAX_ANCESTRY_HOPS = 40


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _is_headless(cmdline: bytes | None) -> bool:
    """True only when we can POSITIVELY prove this is a `claude -p` call.

    Unreadable cmdline returns False (i.e. "treat as a real session"), which is
    the sparing direction — see FAIL DIRECTION above.
    """
    if cmdline is None:
        return False
    args = cmdline.split(b"\x00")
    return b"-p" in args or b"--print" in args


# `comm` is the primary signal and is MEASURED correct today: the shipped
# `claude.exe` reports comm=="claude" on every live session, including one
# whose exe had been replaced by an upgrade. It is still ONE signal about an
# external binary we do not control, and if a future release changed it every
# live slot would read as poisoned — the expensive direction. argv[0] is
# therefore accepted as an alternative. Both are exact-basename matches, so a
# neighbouring tool ("claude-wrapper", "claude-monitor") does not qualify;
# widening here only ever costs a plain attach.
_CLAUDE_NAMES = frozenset({b"claude", b"claude.exe"})


def _is_claude(comm: bytes | None, cmdline: bytes | None) -> bool:
    if comm is not None and comm.strip() in _CLAUDE_NAMES:
        return True
    if cmdline:
        argv0 = cmdline.split(b"\x00", 1)[0]
        if argv0:
            return argv0.rsplit(b"/", 1)[-1] in _CLAUDE_NAMES
    return False


def _ppid_of(proc_root: Path, pid: int) -> int | None:
    """Parent pid from /proc/<pid>/stat.

    The comm field is parenthesised and may itself contain spaces or ')', so the
    fields after it are located from the LAST ')' — splitting on whitespace from
    the left mis-parses any process whose name contains a space.
    """
    raw = _read(proc_root / str(pid) / "stat")
    if raw is None:
        return None
    try:
        after = raw[raw.rindex(b")") + 1 :].split()
        return int(after[1])  # state, ppid, …
    except (ValueError, IndexError):
        return None


def claude_pids(proc_root: Path) -> list[int] | None:
    """Interactive `claude` pids, or None if /proc could not be enumerated."""
    try:
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return None
    found = []
    for entry in entries:
        cmdline = _read(entry / "cmdline")
        comm = _read(entry / "comm")
        if not _is_claude(comm, cmdline):
            continue
        if _is_headless(cmdline):
            continue
        found.append(int(entry.name))
    return found


def liveness(pane_pids: list[int], proc_root: Path = Path("/proc")) -> str:
    """Report whether an interactive claude descends from any of *pane_pids*."""
    if not pane_pids:
        # The door could not tell us which panes to inspect; do not guess.
        return UNKNOWN
    pids = claude_pids(proc_root)
    if pids is None:
        return UNKNOWN
    targets = set(pane_pids)
    # POISONED requires every walk to CONCLUDE (reach init without meeting a
    # pane pid). A walk cut short — hop bound hit, stat unreadable mid-chain —
    # proves nothing, and the broken walk is exactly the one that might have
    # connected claude to the pane; treating it as death is the expensive
    # error. Such runs answer UNKNOWN, which costs a plain attach. One
    # deliberately ACCEPTED consequence: a single process with an unresolvable
    # ancestry (a stat cycle, a >hop-bound chain) suppresses heals box-wide for
    # as long as it lives — that is the fail-direction's price, not a bug.
    inconclusive = False
    for pid in pids:
        if pid in targets:
            return ALIVE  # the pane process IS claude (legacy `exec` shape)
        verdict = _walk_verdict(proc_root, pid, targets)
        if verdict == ALIVE:
            return ALIVE
        if verdict == UNKNOWN:
            inconclusive = True
    return UNKNOWN if inconclusive else POISONED


def _walk_verdict(proc_root: Path, pid: int, targets: set[int]) -> str:
    """One candidate's ancestry, resolved to ALIVE / POISONED / UNKNOWN.

    POISONED here means "this candidate is conclusively NOT the pane's claude"
    — it contributes to (never decides) the session verdict. A candidate that
    VANISHED since enumeration (its /proc dir is gone at hop 0) is exactly as
    conclusive as one that walked to init: an exited process cannot be the
    slot's live claude, and scoring it UNKNOWN would let routine box-wide
    claude churn suppress every heal. A candidate still PRESENT but with an
    unreadable/unparseable stat stays UNKNOWN — it might be ours.
    """
    cur, hops = pid, 0
    while True:
        if hops >= _MAX_ANCESTRY_HOPS:
            return UNKNOWN
        parent = _ppid_of(proc_root, cur)
        if parent is None:
            if hops == 0 and not (proc_root / str(cur)).exists():
                return POISONED  # exited between enumeration and walk
            return UNKNOWN
        if parent <= 1:
            return POISONED  # walked to init: a real conclusion
        if parent in targets:
            return ALIVE
        cur, hops = parent, hops + 1


def idleness(pane_pid: int, proc_root: Path = Path("/proc")) -> str:
    """Is the pane process sitting at a prompt, with no running job?

    Liveness answers "is claude here"; this answers the door's SECOND question
    before it may type: `send-keys C-c` kills whatever foreground job the pane
    shell is running, and the shell's NAME cannot tell an idle prompt from
    `bash script.sh` (both report "bash"). Child-presence can: an idle
    interactive shell has no children, while every running job — vim, rsync,
    a nested shell, a build — is a child of the pane process.

    Same fail direction as liveness: only an affirmative IDLE permits
    keystrokes. A shell that merely holds a BACKGROUND job also reads BUSY —
    over-sparing, but the cost is a plain attach.

    A sibling entry that vanishes between the listing and the read (dir or
    stat already gone) cannot be a LIVE child and is skipped; one whose stat
    is present but unreadable/unparseable might BE the pane's child, so it
    withholds the IDLE verdict instead.
    """
    if pane_pid <= 0:
        return UNKNOWN
    if _read(proc_root / str(pane_pid) / "stat") is None:
        return UNKNOWN  # the pane process itself is not observable
    try:
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return UNKNOWN
    inconclusive = False
    for entry in entries:
        if entry.name == str(pane_pid):
            continue
        try:
            raw = (entry / "stat").read_bytes()
        except FileNotFoundError:
            continue  # exited between listing and read — not a live child
        except OSError:
            inconclusive = True
            continue
        try:
            after = raw[raw.rindex(b")") + 1 :].split()
            ppid = int(after[1])
        except (ValueError, IndexError):
            inconclusive = True
            continue
        if ppid == pane_pid:
            return BUSY
    return UNKNOWN if inconclusive else IDLE


def main(argv: list[str] | None = None) -> int:
    """Print the verdict on line 1 and a human note on line 2.

    Mirrors the stdout protocol of the sibling gates (`session_cap`,
    `login_gate`) so the launcher parses all three the same way. Any internal
    error prints UNKNOWN rather than raising: the caller must never be left
    without a verdict, and UNKNOWN is the sparing one.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0] == "--idle":
            rest = [a for a in args[1:] if a.strip().isdigit()]
            verdict = idleness(int(rest[0])) if rest else UNKNOWN
        else:
            pids = [int(p) for p in args if p.strip().isdigit()]
            verdict = liveness(pids)
    except Exception:  # noqa: BLE001 - a crash here must not break the door
        verdict = UNKNOWN
    notes = {
        ALIVE: "an interactive claude is running in this slot",
        POISONED: "no interactive claude is running in this slot",
        UNKNOWN: "could not determine the slot's state; leaving it untouched",
        IDLE: "the pane shell is at a prompt with no running job",
        BUSY: "the pane shell is running a job; typing would interrupt it",
    }
    print(verdict)
    print(notes[verdict])
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
