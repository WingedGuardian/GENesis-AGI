"""Which commit was the checkout at when genesis-server started?

Usage: serving_commit.py [--held] <reflog> <boot-unix-seconds> <head-sha> <expiry-cutoff>

  <reflog>  the checkout's HEAD reflog file (``<git-dir>/logs/HEAD``)
  <boot>    the unit's ActiveEnterTimestamp, unix seconds (``systemctl --user show
            genesis-server -p ActiveEnterTimestamp --timestamp=unix --value``,
            with its leading ``@`` removed)
  <head>    the checkout's current HEAD
  <cutoff>  unix seconds before which git may already have expired unreachable
            reflog entries (``git config --type=expiry-date
            gc.reflogExpireUnreachable``, or git's 30-day default when unset;
            0 when they never expire). Empty means it could not be read.

Prints the commit and exits 0, or prints ``unknown: <reason>`` and exits 1.

This is the tree the server BOOTED from. A module the server imports later loads
whatever is on disk at that moment, so a server can run a mix; callers say so.
With ``--held`` it prints the boot commit and then every commit HEAD has held
since, one per line, oldest first: any of them may have been imported from, and
a later commit that restores the boot's files does not unload a module, so the
two ends alone cannot say what the server runs.

Each line of the reflog is ``<old> <new> <identity> <unix> <tz>\\t<message>``, one
per move of HEAD, oldest first. The answer is the newest move before the boot,
and it is only trusted when every one of these holds:

  * the moves after it form an unbroken chain to HEAD: each move's OLD commit is
    the previous move's NEW one. A gap means entries were pruned or expired;
  * no move shares the boot's second: both clocks count whole seconds, so a move
    in that second may have come either side of the boot;
  * the boot is newer than the unreachable-entry cutoff. Past it, ``git gc`` may
    have expired a detour's entries as a PAIR (A->F and F->A), which removes the
    commit the server booted from and leaves the chain unbroken;
  * no move written before the chosen one is timed after the boot (the clock
    stepped back across it).

Residuals, stated, where the file cannot show the problem: ``git reflog expire
--rewrite``, or a manual expire with a shorter cutoff than the configured one; a
per-ref ``gc.<pattern>.reflogExpireUnreachable`` override; and a move whose
reflog time was backdated (git records the committer time, so a move made with
``GIT_COMMITTER_DATE`` in the past reads as having happened then, and so does a
move made after the clock stepped back behind the boot). The complete
answer is the server recording its own commit at boot; until then this is the
best evidence on disk. Stdlib only: this runs under the system python.
"""

from __future__ import annotations

import sys


class _Unknown(Exception):
    """The reflog cannot say which commit the server booted from."""


def _moves(text: str) -> list[tuple[str, str, int]]:
    moves = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        head = line.split("\t", 1)[0].split()
        # old, new, then an identity of any number of words, then unix and tz.
        if len(head) < 5:
            raise _Unknown(f"reflog line {n} is not in the documented shape")
        try:
            when = int(head[-2])
        except ValueError as exc:
            raise _Unknown(f"reflog line {n} has no readable time") from exc
        moves.append((head[0], head[1], when))
    return moves


def _since_boot(text: str, boot: int, head: str, cutoff: int) -> list[str]:
    """The boot commit, then the commit each later move of HEAD left it at."""
    if boot < cutoff:
        raise _Unknown(
            "the server booted before git's expiry cutoff for unreachable reflog "
            "entries, which can remove a detour without leaving a gap"
        )
    moves = _moves(text)
    if not moves:
        raise _Unknown("the reflog is empty")
    if moves[-1][1] != head:
        raise _Unknown("the reflog does not end at HEAD")
    at = None
    for i in range(len(moves) - 1, -1, -1):
        if moves[i][2] <= boot:
            at = i
            break
    if at is None:
        raise _Unknown("the reflog starts after the server booted")
    # Whole seconds on both clocks: a move in the boot's own second may have come
    # either side of it. A move that changed nothing (old == new) is harmless.
    if any(when == boot and old != new for old, new, when in moves[: at + 1]):
        raise _Unknown("HEAD moved in the same second the server booted")
    # A move written before this one but timed after the boot means the clock
    # stepped back; the ordering the answer relies on is gone. (A clock that
    # steps back AFTER the boot leaves no such trace: see the residuals above.)
    if any(when > boot for _, _, when in moves[:at]):
        raise _Unknown("reflog times go backwards around the boot")
    for i in range(at + 1, len(moves)):
        if moves[i][0] != moves[i - 1][1]:
            raise _Unknown("the reflog has a gap after the boot (pruned or expired entries)")
    # The chain is unbroken, so each move's NEW commit is every tree HEAD held.
    return [new for _, new, _ in moves[at:]]


def main(argv: list[str]) -> int:
    held = len(argv) > 1 and argv[1] == "--held"
    if held:
        argv = argv[:1] + argv[2:]
    try:
        if len(argv) != 5:
            raise _Unknown(
                "usage: serving_commit.py [--held] <reflog> <boot-unix> <head> <expiry-cutoff>"
            )
        try:
            boot = int(argv[2])
        except ValueError as exc:
            raise _Unknown(f"the boot time is not a number of seconds ({argv[2]!r})") from exc
        if boot <= 0:
            raise _Unknown("the server has no recorded start")
        try:
            cutoff = int(argv[4])
        except ValueError as exc:
            raise _Unknown(
                "cannot read git's reflog expiry for unreachable entries "
                "(gc.reflogExpireUnreachable)"
            ) from exc
        try:
            with open(argv[1], encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise _Unknown(f"cannot read the reflog ({exc.strerror})") from exc
        commits = _since_boot(text, boot, argv[3], cutoff)
        print("\n".join(commits) if held else commits[0])
        return 0
    except _Unknown as exc:
        print(f"unknown: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
