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
    for pid in pids:
        if pid in targets:
            return ALIVE  # the pane process IS claude (legacy `exec` shape)
        cur, hops = pid, 0
        while hops < _MAX_ANCESTRY_HOPS:
            parent = _ppid_of(proc_root, cur)
            if parent is None or parent <= 1:
                break
            if parent in targets:
                return ALIVE
            cur, hops = parent, hops + 1
    return POISONED


def main(argv: list[str] | None = None) -> int:
    """Print the verdict on line 1 and a human note on line 2.

    Mirrors the stdout protocol of the sibling gates (`session_cap`,
    `login_gate`) so the launcher parses all three the same way. Any internal
    error prints UNKNOWN rather than raising: the caller must never be left
    without a verdict, and UNKNOWN is the sparing one.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        pids = [int(p) for p in args if p.strip().isdigit()]
        verdict = liveness(pids)
    except Exception:  # noqa: BLE001 - a crash here must not break the door
        verdict = UNKNOWN
    notes = {
        ALIVE: "an interactive claude is running in this slot",
        POISONED: "no interactive claude is running in this slot",
        UNKNOWN: "could not determine slot liveness; treating it as running",
    }
    print(verdict)
    print(notes[verdict])
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
