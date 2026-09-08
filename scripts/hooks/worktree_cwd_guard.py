#!/usr/bin/env python3
"""PreToolUse hook: worktree safety guard (removal protection + relocation block).

Three modes:
  1. Bash matcher (default) — intercepts `git worktree remove` commands.
  2. ExitWorktree matcher (--exit-worktree) — intercepts ExitWorktree tool
     with action "remove".
  3. EnterWorktree matcher (--enter-worktree) — hard-blocks the EnterWorktree
     tool, which would RELOCATE the session into a worktree and make the
     conversation unfindable via /resume (see _handle_enter_worktree).

Removal modes (1 + 2):
  - If another process has its CWD inside the target worktree → hard block
    with PID list (cross-session safety).
  - If the current session's CWD IS the target → hard block (self-brick
    prevention).
  - If no conflicts → still block. Worktrees are never removed directly.
    The lifecycle manager (scripts/worktree_lifecycle.py) handles cleanup
    via a trash bin with 7-day recovery.

Incident 1: 2026-05-27 — Session bricked after deleting its own worktree.
Incident 2: 2026-06-09 — Session B deleted worktree still used by Session A,
turning A into a zombie.
Incident 3: 2026-06-29 — EnterWorktree silently relocated a multi-day session
into the `morning-report-nextsteps` worktree; its transcript moved to a
separate Claude Code project slug, so /resume from the main repo no longer
listed it (11 such `wt-*` relocation stubs had accumulated).

Stdlib-only. Fail-open on parse errors.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import read_payload, run_guard, tool_input  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze,
    git_subcommand_index,
    untokenizable,
)

# Kept ONLY for the untokenizable fallback below and as a cheap pre-gate — never
# as the verdict. As the verdict it was quote-blind: it matched the phrase inside
# a quoted grep pattern, a heredoc body or a commit message, and target
# extraction then read the following word as a path, so a read-only search was
# refused with "use the lifecycle manager". Observed over 51,052 (command,
# directory) pairs, replayed from the directory each was typed in: this coarse
# predicate blocks 154, the parser below also blocks 154, and 148 are common —
# so the swap releases 6 mentions and catches 6 real removals.
#
# PROVENANCE, because the distinction matters: this is one operator's local
# session history at 2026-09-03, not a published result. The corpus is built
# from real transcripts and holds secrets passed in argv, so it can never be
# checked in and no reader or fork can reproduce or falsify these totals. Treat
# them as the SCALE at which the swap was observed. The composition is the
# claim; the totals are context.
#
# The numbers here churned twice, which is the reason for the precision: an
# earlier draft said 147/3, a later one 150/146/4 over a 48,363-command corpus
# that replayed everything from the repo root. The classifier producing them has
# its own blind spot (a commit message whose markdown backticks parse as command
# substitution), so treat the composition as the claim and the total as context.
#
# It does NOT require `git` adjacent to the subcommand, and that omission is the
# point. It used to (`\bgit\s+worktree\s+remove\b`), which carried into the
# untokenizable fallback the very assumption the parsed route had already been
# fixed for: `git -C <dir> worktree remove <target>` produced no target, so a
# command nothing could tokenize fell OPEN. Cross-model review, 2026-09-06.
# Modelling git's global-option grammar here instead — which options take a
# value — is the open-set claim this file refuses to make in a regex, so the
# assumption is DELETED rather than extended. Anchoring on the subcommand and
# the operation alone can over-match and never under-match, and both readers of
# this pattern (the untokenizable fallback, and the carrier gate) are branches
# where over-blocking is the declared correct side.
_WORKTREE_REMOVE = re.compile(r"\bworktree\s+remove\b")
# Dropping `git` from the pattern above is right for the untokenizable branch and
# WRONG for the carrier branch, which fires on commands that parse perfectly.
# MEASURED: without this, `rg 'worktree remove' -l | xargs wc -l` and
# `ssh box 'ls' && echo 'the worktree remove doc'` — a read-only search and a line
# of prose — both began to block, and `_legacy_targets` then invented the target
# ("Cannot remove worktree 'runbook'"). That is the exact harm this branch exists
# to remove, so the carrier branch keeps a `git` token as a separate conjunct
# instead. It is checked as raw text, not as a parsed executable, because in the
# shapes that branch covers the removal is INSIDE a quoted string and has no
# segment of its own.
_GIT_TOKEN = re.compile(r"\bgit\b")
_SUBCOMMAND = "worktree"
_OPERATION = "remove"

# Executables that CARRY a command string ``analyze`` does not descend into, and
# the shell function-definition syntax, which hides one the same way.
#
# This is a SECOND blind spot, distinct from the one ``untokenizable`` covers:
# `eval '<removal>'`, `ssh box "<removal>"`, `find -exec`, `parallel`, `watch`,
# `script -c` all tokenize PERFECTLY. The parser reads the carrier as the
# executable, skips the segment, finds no target, and the removal is allowed.
# Observed against the pre-parser version: 9 real removals it blocked and the
# parser let through (same local corpus and caveat as above).
#
# This list IS an open set, and unlike the shell-side arms that fact is
# tolerable here, because of the direction it fails in: a carrier absent from
# the list costs a missed block (bad, but no worse than not having the list),
# while every ADDITION only ever routes more commands to the coarse extractor.
# The list grows toward safety, which is why adding an entry is a one-line
# change with no design question attached.
#
# Read that as a claim about DIRECTION, not about cost. The entries present were
# observed cheap on one install's history; that measurement is not something a
# later contributor can repeat, so the safety of an addition rests on the
# argument above — it can only route MORE commands to the coarse extractor —
# and never on a number. Do not add one believing its cost has been checked.
_CARRIER_NAMES = frozenset(
    {"eval", "ssh", "find", "parallel", "watch", "script", "su", "docker", "flock", "xargs"}
)
# The raw-text half. It stays because it is the only thing that sees a shell
# FUNCTION DEFINITION, which has no executable to resolve.
_COMMAND_CARRIER = re.compile(
    r"(?:^|[\s;&|(])(?:" + "|".join(sorted(_CARRIER_NAMES)) + r")(?:\s|$)"
    r"|[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)\s*\{"
)


def _legacy_targets(cmd: str) -> list[str]:
    """The pre-parser extractor, kept ONLY for the untokenizable fallback.

    Quote-blind — it matches the phrase anywhere and reads the following word as
    a path — which is exactly why it is not the primary route any more. But when
    shlex cannot tokenize the command there is nothing better available, and this
    guard's operation can strand a session, so the coarse reading is the correct
    fail-closed behaviour there rather than a silent allow.
    """
    targets: list[str] = []
    for match in _WORKTREE_REMOVE.finditer(cmd):
        for token in cmd[match.end() :].split():
            token = token.strip("'\"")
            if not token or token.startswith("-"):
                continue
            targets.append(token)
            break
    return targets


def _extract_worktree_targets(cmd: str) -> list[str]:
    """Target paths of every EXECUTED worktree-removal segment.

    Keyed on parsed structure, not on the phrase appearing in the text: the
    segment's resolved executable must be ``git``, its subcommand ``worktree``,
    and the next positional ``remove``. Operands come from ``seg.argv``, which is
    already shlex-dequoted — the old ``rest.split()`` + ``strip("'\\"")`` mangled
    any quoted path containing a space.

    ``analyze`` flattens nested ``bash -c`` bodies, so a wrapped removal is still
    seen; depth is deliberately NOT filtered, because a removal inside
    ``bash -c`` really does execute. It does NOT descend into a command string
    handed to an ordinary executable (``eval``, ``ssh host "…"``, ``find -exec``)
    — see ``_COMMAND_CARRIER`` and the fallback in ``_handle_bash``, which is
    what covers that class.
    """
    targets: list[str] = []
    for seg in analyze(cmd):
        if seg.exe != "git":
            continue
        # The INDEX from the parser's own scan, never `argv.index(_SUBCOMMAND)`:
        # that returns the first token equal to the name, and a global option's
        # operand can BE that name. `git -C worktree worktree remove /tmp/x` anchored on
        # the `-C` operand, so `after_sub[0]` was the literal "worktree" instead of
        # "remove", the segment was skipped, and a real removal was ALLOWED.
        sub_idx = git_subcommand_index(seg.argv)
        if sub_idx is None or seg.argv[sub_idx] != _SUBCOMMAND:
            continue
        after_sub = seg.argv[sub_idx + 1 :]
        if not after_sub or after_sub[0] != _OPERATION:
            continue
        for token in after_sub[1:]:
            if token.startswith("-"):
                continue  # a flag such as --force
            targets.append(token)
            break  # the removal takes one path
    return targets


def _carries_a_command(cmd: str) -> bool:
    """Whether ``cmd`` hands a command STRING to something ``analyze`` skips.

    Asks the PARSER first, and the raw text only as a fallback. The regex above
    requires the carrier's name to follow the start of the string or one of a few
    separators, so ``/`` did not end the preceding word and a path-qualified
    carrier — ``/usr/bin/find … -exec <removal>`` — matched nothing and the
    removal was allowed. Cross-model review, 2026-09-06.

    Patching the regex would have fixed ``find`` and left the other nine names
    for the next round. ``analyze`` has already resolved and BASENAMED every
    segment's executable by this point, so consulting it closes the whole list at
    once, for every spelling of a path, with no new name to guess. The regex is
    still consulted because a shell function definition has no executable at all.
    """
    if any(seg.exe in _CARRIER_NAMES for seg in analyze(cmd)):
        return True
    return bool(_COMMAND_CARRIER.search(cmd))


def _resolve_path(path: str) -> str:
    """Resolve a path to its absolute, real form."""
    expanded = os.path.expanduser(path)
    return os.path.realpath(os.path.abspath(expanded))


def _find_processes_in_dir(dir_path: str) -> list[int]:
    """Return PIDs (excluding self and parent) with CWD inside dir_path.

    Scans /proc/[0-9]*/cwd symlinks. Each readlink is a single syscall.
    Processes that vanish between enumeration and readlink are silently
    skipped. ~250ms for ~100 processes on this container.

    Excludes own PID and parent PID. The parent is the CC session that
    fired this hook (genesis-hook uses exec, so the hook's ppid IS the
    CC process). Excluding it avoids false positives from the current
    session's own process chain.
    """
    exclude = {os.getpid(), os.getppid()}
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids  # fail-open
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            # Normalize — readlink may return path with trailing " (deleted)"
            # for processes whose CWD has been removed, but we care about
            # preventing removal, so at PreToolUse time the dir still exists.
            if cwd == dir_path or cwd.startswith(dir_path + "/"):
                pids.append(pid)
        except (OSError, PermissionError, FileNotFoundError):
            continue
    return pids


def _block_with_pids(target: str, pids: list[int]) -> int:
    """Print cross-session block message and return exit code 2."""
    pid_str = ", ".join(str(p) for p in pids[:10])
    if len(pids) > 10:
        pid_str += f" (+{len(pids) - 10} more)"
    print(
        f"BLOCKED: Cannot remove worktree '{target}' — "
        f"{len(pids)} other process(es) have their working directory inside it.",
        file=sys.stderr,
    )
    print(f"PIDs: {pid_str}", file=sys.stderr)
    print(
        "This would brick those sessions. Wait for them to finish or use the lifecycle manager.",
        file=sys.stderr,
    )
    return 2


def _block_no_direct_removal(target: str) -> int:
    """Print lifecycle-manager redirect and return exit code 2."""
    print(
        "BLOCKED: Direct worktree removal is disabled.",
        file=sys.stderr,
    )
    print(
        "Worktrees are managed by the lifecycle manager "
        "(scripts/worktree_lifecycle.py) which uses a trash bin "
        "with 7-day recovery.",
        file=sys.stderr,
    )
    print(
        "The lifecycle manager runs daily via cron. To manually trigger: "
        "python scripts/worktree_lifecycle.py --dry-run",
        file=sys.stderr,
    )
    return 2


def _handle_bash(data: dict) -> int:
    """Handle Bash tool — intercept `git worktree remove` commands."""
    cmd = data.get("command", "")
    if not cmd:
        return 0

    # Cheap pre-gate: cost only. Correctness rests on the parser below, never on
    # this substring.
    if _SUBCOMMAND not in cmd:
        return 0

    # A command shlex cannot tokenize gets the PREVIOUS, coarser reading rather
    # than a silent allow. analyze() degrades to a naive split without SAYING so,
    # so "no removal segment found" and "no removal present" are indistinguishable
    # in its output; untokenizable() is the only way to tell them apart, and this
    # guard covers an operation that can strand a session, so the unreadable case
    # keeps failing CLOSED.
    #
    # The fallback has to use the LEGACY extractor, not the parser: a first cut
    # gated on the regex here and then called the parser anyway, which returned []
    # on precisely the input the parser could not read, so the loop never ran and
    # the command was allowed — a branch whose comment claimed fail-closed while
    # the code fell open. `echo 'it's fine' ; git worktree remove /wt/X` is the
    # shape (an unbalanced quote collapses everything into one echo segment).
    if untokenizable(cmd):
        targets = _legacy_targets(cmd)
    else:
        targets = _extract_worktree_targets(cmd)
        # The text says a removal and the parser found none, in a command that
        # hands a command STRING to something. That is the carrier class: it
        # tokenizes cleanly, so the probe above cannot see it, and the parser
        # reads the carrier as the executable and skips the segment. Fall back
        # to the coarse extractor rather than allow — this operation strands a
        # session and cannot be undone.
        #
        # Ordered deliberately: the carrier test runs ONLY when the parser found
        # nothing, so a normally-parsed command never touches it, and a mention
        # inside `grep` / `echo` / `git commit -m` is unaffected because no
        # carrier is present.
        #
        # A mention CAN carry one, though — `ssh box 'ls' && echo 'the worktree
        # remove doc'` is prose next to a carrier, and `rg '<phrase>' -l | xargs
        # wc -l` is a read-only search next to one. The `git` token is what
        # separates those from the shapes this branch is for; every carried
        # removal names the executable it is about to run, and none of the
        # mentions above does. MEASURED: five such commands blocked without this
        # conjunct and are allowed with it, while all three carrier controls
        # (`eval`, `ssh`, `find -exec`) keep blocking.
        #
        # The "+2 blocks (0.004%) over 51,052 commands" figure this comment used
        # to carry was measured against a NARROWER pattern and a raw-text-only
        # carrier test, both of which have since widened. It is not re-derivable
        # (the corpus is one install's transcripts) so it has been removed rather
        # than restated for a gate it no longer describes.
        if (
            not targets
            and _WORKTREE_REMOVE.search(cmd)
            and _GIT_TOKEN.search(cmd)
            and _carries_a_command(cmd)
        ):
            targets = _legacy_targets(cmd)
    if not targets:
        return 0

    # Get the current working directory
    cwd = os.getcwd()
    cwd_real = os.path.realpath(cwd)
    for target in targets:
        target_real = _resolve_path(target)

        # Check 1: Self-CWD — would brick this session
        if cwd_real == target_real or cwd_real.startswith(target_real + "/"):
            print(
                f"BLOCKED: Cannot remove worktree '{target}' — "
                f"it is your current working directory.",
                file=sys.stderr,
            )
            print(
                "This would brick the session (every Bash command would "
                "fail with 'Path does not exist').",
                file=sys.stderr,
            )
            return 2

        # Check 2: Cross-session — another process is using it
        other_pids = _find_processes_in_dir(target_real)
        if other_pids:
            return _block_with_pids(target, other_pids)

        # Check 3: No conflict — still block, redirect to lifecycle manager
        return _block_no_direct_removal(target)

    # Fallthrough (no targets parsed) — shouldn't happen, but fail-open
    return 0


def _handle_exit_worktree(data: dict) -> int:
    """Handle ExitWorktree tool — block action "remove"."""
    action = data.get("action", "")
    if action != "remove":
        return 0  # "keep" is always allowed

    # The session is still in the worktree at PreToolUse time
    cwd = os.getcwd()
    cwd_real = os.path.realpath(cwd)

    # Check for other processes in this worktree
    other_pids = _find_processes_in_dir(cwd_real)
    if other_pids:
        return _block_with_pids(cwd, other_pids)

    # No conflict — still block, redirect to "keep"
    print(
        "BLOCKED: Direct worktree removal is disabled.",
        file=sys.stderr,
    )
    print(
        "Use ExitWorktree with action 'keep' instead. "
        "The lifecycle manager (scripts/worktree_lifecycle.py) handles "
        "cleanup automatically via a trash bin with 7-day recovery.",
        file=sys.stderr,
    )
    return 2


def _handle_enter_worktree(data: dict) -> int:
    """Handle EnterWorktree tool — hard-block to keep sessions findable.

    EnterWorktree re-roots the live session into a git worktree: the harness
    mints a NEW session id whose transcript is written under a DIFFERENT Claude
    Code project slug (``…<repo>--claude-worktrees-<name>/``), leaving only a
    ``wt-<id>.jsonl`` pointer stub behind in the original project dir. The
    conversation continues seamlessly on screen, but ``/resume`` launched from
    the original directory no longer lists it — the session is, in effect, lost.

    Worktree *isolation* never requires relocating the session, so block
    unconditionally and redirect to non-relocating alternatives. Always returns
    exit code 2 regardless of input (``name`` / ``path`` / empty).
    """
    target = "(auto-named worktree)"
    if isinstance(data, dict):
        target = data.get("name") or data.get("path") or target
    print(
        f"BLOCKED: EnterWorktree is disabled — entering '{target}' would "
        "relocate this session into a worktree and make it unfindable.",
        file=sys.stderr,
    )
    print(
        "Why: the harness re-roots the session and writes its transcript under "
        "a separate '<repo>--claude-worktrees-<name>' project dir, leaving only "
        "a 'wt-<id>.jsonl' stub behind. /resume from the original directory will "
        "no longer list this conversation.",
        file=sys.stderr,
    )
    print("Keep the session findable — do this instead:", file=sys.stderr)
    print(
        "  - Isolated file changes: `git worktree add .claude/worktrees/<name> "
        "-b <scope>/<desc> origin/main`, then edit via the worktree's ABSOLUTE "
        "paths and test with `PYTHONPATH=<worktree>/src pytest <files>`. Your "
        "session stays in the main repo and in /resume.",
        file=sys.stderr,
    )
    print(
        "  - Parallel isolated work: dispatch a subagent (Agent tool, "
        'isolation="worktree") — the child runs in its own worktree; your '
        "session is untouched.",
        file=sys.stderr,
    )
    print(
        "  - If a worktree-ROOTED session is genuinely wanted, the USER should "
        "launch Claude Code from that directory, so it is findable there from "
        "the start.",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    # EnterWorktree is hard-blocked UNCONDITIONALLY: it relocates the session
    # regardless of arguments or input, so block before any parse that could
    # otherwise fail-open (empty/missing/malformed payload) and let
    # the relocation through. Input is parsed only to name the worktree in the
    # message; failure to parse still blocks.
    if "--enter-worktree" in sys.argv:
        return _handle_enter_worktree(tool_input(read_payload()))

    try:
        # Handlers operate on the tool-input dict (command / action fields).
        data = tool_input(read_payload())

        # Determine mode from CLI args
        if "--exit-worktree" in sys.argv:
            return _handle_exit_worktree(data)
        else:
            return _handle_bash(data)

    except (json.JSONDecodeError, KeyError, OSError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    run_guard(main, "worktree_cwd_guard")
