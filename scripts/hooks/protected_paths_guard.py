#!/usr/bin/env python3
"""PreToolUse hook (Bash): block rm/rmdir OPERANDS that target protected data.

Complements destructive_command_guard.py (depth-based) with an explicit
blocklist of named paths containing irreplaceable data: session transcripts,
encrypted backups, Qdrant snapshots, browser profiles, and the production
database.

Operand-aware (2026-08 rewrite): the old check was `if path in cmd` — a raw
substring test that blocked any command MENTIONING a protected path alongside
any rm (`rm scratch.txt; cat ~/backups/notes`) and blocked deleting a file
INSIDE a protected dir, contradicting this docstring. Now the rm/rmdir
segments are parsed via shell_parse.analyze (the same quote-aware parser the
push gate uses — no second divergent tokenizer) and only real deletion
TARGETS block:

  * the protected dir itself (`rm -rf ~/genesis/data`)
  * an ancestor of it (`rm -rf ~/genesis` — removes data/ as a side effect)
  * a glob that could match the dir or an ancestor (`rm -rf ~/genesis/da*`)
  * ANY glob under the dir (`rm -rf ~/genesis/data/*`, `…/data/*.db` — wipes
    the contents while dodging "the dir itself")
  * a protected FILE (the production DB + its WAL/SHM sidecars — deleting the
    -wal of a live SQLite DB silently loses committed transactions)

A specific non-glob file inside (`rm ~/genesis/data/old.log`) and non-rm
mentions (`cat ~/backups/notes`) are ALLOWED.

Relative operands resolve against the payload's `cwd`. Limitation (documented,
not hidden): a `cd` INSIDE a compound command shifts the real cwd and is not
tracked here — same posture as shell_parse: this is an approval/friction
layer, not a sandbox. The old substring check missed that case too.

Fail modes: an untokenizable command (shlex error) falls back to the legacy
substring check — conservative, never weaker than the old guard. An unexpected
crash fails CLOSED via hook_input.run_guard (exit 2).
"""

from __future__ import annotations

import os
import re
import sys
from fnmatch import fnmatch

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import brace_expand, read_payload, run_guard, tool_input  # noqa: E402
from shell_parse import analyze, untokenizable  # noqa: E402

# Directories that must never be deleted.  Relative to $HOME.
_PROTECTED_RELATIVE = [
    ".claude/projects",  # CC session transcripts (JSONL)
    "backups",  # Encrypted Genesis backups
    "snapshots",  # Qdrant snapshots
    ".genesis/camoufox-profile",  # Camoufox browser profile
    ".genesis/browser-profile",  # Chromium browser profile
    "genesis/data",  # Production database (genesis.db)
]

# Individual files that must never be deleted even though files inside their
# parent dir are otherwise deletable. The WAL/SHM sidecars ride along: deleting
# the -wal of a live SQLite database silently discards committed-but-
# uncheckpointed transactions.
_PROTECTED_FILES_RELATIVE = [
    "genesis/data/genesis.db",
    "genesis/data/genesis.db-wal",
    "genesis/data/genesis.db-shm",
]

# Matches rm or rmdir as a word boundary (fast path + legacy fallback trigger)
_RM_PATTERN = re.compile(r"\brm\b|\brmdir\b")

# Glob metacharacters an rm operand may carry (bash expands these before rm
# runs, so the guard must reason about what they COULD match).
_GLOB_CHARS = ("*", "?", "[")

# A redirection-shaped token to skip (mirrors destructive_command_guard's
# _REDIR_TOKEN): optional fd digits then < or >, or &>. Never a real rm target
# (no dangerous path starts with a redirection operator).
_REDIR_TOKEN = re.compile(r"\d*[<>]|&>")


def _expand(token: str, cwd: str | None) -> str | None:
    """Absolute, normalized form of an rm operand, or None if unresolvable.

    $HOME/… and ~/… forms expand; a relative operand resolves against the
    payload cwd. shlex already stripped quotes, mirroring bash: a single-quoted
    '$HOME' never reached us as a variable in bash either.
    """
    expanded = os.path.expanduser(os.path.expandvars(token))
    if not os.path.isabs(expanded):
        if not cwd or not isinstance(cwd, str):
            return None  # relative target, unknown base — caller falls back
        expanded = os.path.join(cwd, expanded)
    return os.path.normpath(expanded)


def _has_glob(token: str) -> bool:
    return any(ch in token for ch in _GLOB_CHARS)


def _operand_blocks(operand: str, cwd: str | None, dirs: list[str], files: list[str]) -> str | None:
    """Reason string if deleting ``operand`` would destroy protected data."""

    expanded = _expand(operand, cwd)
    if expanded is None:
        return None  # unresolvable relative operand — handled by caller fallback
    is_glob = _has_glob(operand)
    for prot in dirs:
        if expanded == prot:
            return f"'{operand}' is the protected directory {prot}"
        if prot.startswith(expanded + "/"):
            return f"'{operand}' is an ancestor of the protected directory {prot}"
        if is_glob and fnmatch(prot, expanded):
            return f"glob '{operand}' can match the protected directory {prot}"
        if is_glob and expanded.startswith(prot + "/"):
            return f"glob '{operand}' deletes contents of the protected directory {prot}"
    for prot in files:
        if expanded == prot:
            return f"'{operand}' is the protected file {prot}"
        if is_glob and fnmatch(prot, expanded):
            return f"glob '{operand}' can match the protected file {prot}"
    return None


def _rm_operands(argv: list[str]) -> list[str]:
    """The operand (non-flag, non-redirect) tokens of an rm/rmdir argv."""
    operands: list[str] = []
    flags_done = False
    for tok in argv[1:]:
        if not flags_done and tok == "--":
            flags_done = True
            continue
        if not flags_done and tok.startswith("-") and len(tok) > 1:
            continue
        if _REDIR_TOKEN.match(tok):
            continue
        operands.append(tok)
    return operands


# Commands whose job is to run ANOTHER command. A deletion behind one is still a
# deletion, so a segment led by one must not be certified safe merely because its
# resolved executable is not `rm`.
#
# Deliberately a superset of shell_parse's wrapper table, and deliberately not
# claimed to be complete: this drives a CONSERVATIVE check, so a member that does
# not belong costs a refusal only when a protected path is already present, while
# a missing member costs nothing that was not already missing. That asymmetry is
# why it is safe to be generous here and precise there.
_EXECUTION_PREFIXES = frozenset(
    {
        "eval",
        "coproc",
        "builtin",
        "command",
        "exec",
        "sudo",
        "doas",
        "su",
        "runuser",
        "setpriv",
        "chroot",
        "env",
        "nice",
        "ionice",
        "chrt",
        "nohup",
        "setsid",
        "stdbuf",
        "time",
        "timeout",
        "xargs",
        "flock",
        "unshare",
        "systemd-run",
        "script",
        "watch",
        "parallel",
        "proot",
        "torsocks",
        "strace",
        "ltrace",
        # Shell names are deliberately absent: `shell_parse` already DESCENDS into
        # `sh -c '…'` and yields the inner command as its own segment, so the loop
        # below sees any deletion there without help. Listing them added no
        # coverage and cost a false refusal.
    }
)


# The subset that RE-PARSES a single argument string rather than passing argv
# through. No wrapper table can resolve these: the command arrives as one opaque
# token, so whatever executable comes back is a fragment of it. Kept small and
# separate from the set above, because this one drives a check that fires even
# when resolution appeared to succeed.
#
# `source` and `.` are deliberately NOT here, though they do run other commands.
# They execute a FILE: there is no command in the argv to unwrap, so being
# conservative about the argv buys nothing — whatever runs is in a file this
# guard cannot read either way. MEASURED: including them refused 11 of 137
# reachable real commands, every one benign (`source .venv/bin/activate` before a
# heredoc that happens to mention both a path and a deletion word — including this
# work's own probe scripts). Zero coverage, real cost.
_REPARSING_PREFIXES = frozenset({"eval", "coproc"})


def _leading_word(raw: str) -> str:
    """Basename of a segment's first raw word, or "" when there is none.

    Uses the RAW text rather than the parsed argv because the shapes this is for
    are the ones whose argv resolution went elsewhere.
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    first = stripped.split(None, 1)[0]
    return os.path.basename(first.strip("'\""))


def _deletion_tail(argv: list[str]) -> list[str] | None:
    """``argv`` from the first deletion word onward, when one runs behind a prefix.

    The prefix itself is unresolved by definition here, so its OWN options cannot
    be modelled — and they must not have to be. Rather than trying to find where
    the prefix stops and the command starts (`setpriv --reuid 1000 rm …` defeats
    any "skip the dashed words" rule the moment an option takes a value), look for
    the deletion word anywhere in the argv and read from there.

    That over-detects by construction, and the direction is deliberate: the cost
    of a false hit is a refusal on a command that contains the literal word `rm`
    AND a protected path AND an unresolved execution prefix; the cost of a miss is
    the data. A word that is merely an OPERAND of something else (`grep rm file`)
    reaches this only when it also sits behind such a prefix, and the operand scan
    that follows then has to find a protected path in it before anything is
    refused.
    """
    for i, tok in enumerate(argv):
        if os.path.basename(tok) in ("rm", "rmdir"):
            return argv[i:]
    return None


def _prefixed_deletion_reason(
    seg, cwd: str | None, dirs: list[str], files: list[str]
) -> str | None:
    """Why a deletion running behind an UNRESOLVED execution prefix is refused.

    Two branches, and which one applies is decided by whether a deletion is
    READABLE in the segment's argv:

    * it is — then this is an ordinary deletion that merely happens to sit behind
      a prefix, so it gets the ORDINARY operand analysis: brace expansion,
      ancestors, globs, unresolved-variable fallback. Anything less re-creates the
      substring check's blind spot for the operand forms it cannot express.
    * it is not — the command is opaque (a re-parsing prefix hands over one quoted
      token), so fall back to the conservative substring check, scoped to THIS
      SEGMENT rather than the whole command line, and only at top level.
    """
    tail = _deletion_tail(seg.argv)
    if tail is None:
        if seg.depth != 0:
            return None
        if _legacy_substring_block(seg.raw, dirs):
            return (
                "a command that runs another command could not be resolved to what it "
                "actually executes, and a protected path appears in it. Rewrite it so "
                "the command being run is visible — or, if the path is only being "
                "mentioned, write that text with an editor rather than through a shell."
            )
        return None

    unresolved = False
    for raw_operand in _rm_operands(tail):
        for operand in brace_expand(raw_operand):
            reason = _operand_blocks(operand, cwd, dirs, files)
            if reason:
                return f"{reason} — and it is being deleted behind '{seg.exe or _leading_word(seg.raw)}', which runs the command that follows it"
            if _expand(operand, cwd) is None:
                unresolved = True
    if unresolved and _legacy_substring_block(seg.raw, dirs):
        return (
            "a deletion behind a command-running prefix has a target this guard "
            "cannot resolve, and a protected path appears in the same command. "
            "Rewrite it with the target spelled out."
        )
    return None


def _legacy_substring_block(cmd: str, dirs: list[str]) -> str | None:
    """The pre-2026-08 substring check — kept ONLY as the fallback for a
    command shlex cannot tokenize (conservative: over-blocks, never under)."""
    home = os.path.expanduser("~")
    for prot in dirs:
        for alias in (prot, prot.replace(home, "~", 1), prot.replace(home, "$HOME", 1)):
            if alias in cmd:
                return f"protected path {alias} appears in an untokenizable rm command"
    return None


def _protected_dirs() -> list[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, rel) for rel in _PROTECTED_RELATIVE]


def _protected_files() -> list[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, rel) for rel in _PROTECTED_FILES_RELATIVE]


def _block(reason: str) -> int:
    print(f"BLOCKED: {reason}.", file=sys.stderr)
    print(
        "This target holds irreplaceable data (session transcripts, backups, "
        "snapshots, browser profiles, or the production database).",
        file=sys.stderr,
    )
    print(
        "Specific files inside a protected directory can be removed by naming "
        "them exactly (no globs).",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    payload = read_payload()
    cmd = tool_input(payload).get("command", "")
    if not cmd or not isinstance(cmd, str):
        return 0

    # Fast path: no rm/rmdir word anywhere in the command.
    if not _RM_PATTERN.search(cmd):
        return 0

    dirs = _protected_dirs()
    files = _protected_files()

    # Explicit tokenizability probe, now SHARED rather than inline here.
    # shell_parse._argv silently degrades to a naive split on shlex errors, so
    # analyze() alone can never signal one.
    #
    # (An earlier draft of this comment called it "the third hand-rolled copy,
    # already drifted from one another". Not true of the tree this lands on: on
    # the default branch this was the ONLY probe of the discard-the-tokens kind.
    # Every other shlex call in scripts/hooks USES its tokens, which is a
    # different act with a different fail direction. A comment describing a tree
    # the change is not landing on is how a reader inherits a false picture.)
    #
    # Not byte-identical to the inline probe it replaces: that one folded
    # `\<newline>` to a space first. The shared probe reads the command RAW,
    # because bash REMOVES a line continuation rather than replacing it, so the
    # fold produced the reading furthest from what actually executes. MEASURED
    # over 12,099 real commands: the two classify identically (339 un-tokenizable
    # either way, zero commands differ), so this is a semantics correction with
    # no observed behaviour change.
    if untokenizable(cmd):
        reason = _legacy_substring_block(cmd, dirs)
        return _block(reason) if reason else 0

    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    segs = analyze(cmd)

    # An EXECUTION PREFIX runs the command that follows it, so a deletion behind
    # one is a deletion. `shell_parse`'s wrapper table resolves many of them, but
    # a table is a WHITELIST: it fails open on its complement, and the complement
    # is open-ended (`setpriv`, `systemd-run`, `unshare`, `flock`, `runuser`,
    # `script -c`, …), as are the spellings a table cannot express at all — a
    # prefix that re-parses a quoted argument, or takes a compound rather than a
    # simple command. Those all resolve to something that is not `rm`, and the
    # loop below would skip them in silence.
    #
    # So do not certify. Fire ONLY where resolution actually failed, which is the
    # whole point — a prefix the parser resolved tells us exactly what runs, and
    # if that is not a deletion there is nothing to be conservative about. Two
    # failure shapes:
    #
    #   * the leading word is a prefix the wrapper table does not know, so
    #     nothing was unwrapped and the resolved exe IS still that prefix; or
    #   * the leading word RE-PARSES its argument, which no wrapper table can
    #     resolve — the command arrives as one opaque token, so the exe that
    #     comes back is a fragment of it rather than the executable.
    #
    # An earlier revision keyed on "leading word is any prefix" and MEASURED as a
    # bad over-block: `sudo grep -r '<pattern>' <protected-dir>` was refused,
    # along with three other ordinary shapes, because `sudo` is both extremely
    # common and already resolved correctly. Firing on a resolved prefix buys
    # nothing and costs real commands.
    #
    # PER SEGMENT, AND THAT IS THE CORRECTION (Codex P1 x3, PR #1615). The first
    # version asked its three questions of the WHOLE COMMAND, and each one let a
    # real deletion through:
    #
    #   * "does ANY segment resolve to rm?" — one unrelated deletion in /tmp
    #     earlier on the line switched the fallback off for every other segment;
    #   * a substring scan for a literal protected-path alias — which by
    #     construction cannot see the operand forms `_operand_blocks` exists to
    #     catch, so an ANCESTOR of every protected directory passed;
    #   * top-level segments only — but `sh -c '…'` surfaces its inner command as
    #     a DEPTH-1 segment, so the same deletion one level down passed.
    #
    # The depth filter had a real reason and it is kept where it belongs: on the
    # SUBSTRING branch only. MEASURED — without it, a 40,925-character heredoc
    # writing a plan document was refused, because two lines deep inside the prose
    # began with a shell name. Reading the OPERANDS of a deletion that is actually
    # in the argv is a different act from scanning text for a path, so that runs
    # at any depth; scanning prose stays at depth 0.
    for seg in segs:
        if seg.exe in ("rm", "rmdir"):
            continue  # resolved — the loop below judges it on its own operands
        if not (seg.exe in _EXECUTION_PREFIXES or _leading_word(seg.raw) in _REPARSING_PREFIXES):
            continue
        reason = _prefixed_deletion_reason(seg, cwd, dirs, files)
        if reason:
            return _block(reason)

    for seg in segs:
        if seg.exe not in ("rm", "rmdir"):
            continue
        seg_unresolved = False
        for raw_operand in _rm_operands(seg.argv):
            # Bash brace-expands an unquoted operand before rm runs, so check
            # each real target (rm -rf ~/genesis/{data,logs} → …/data, …/logs).
            for operand in brace_expand(raw_operand):
                reason = _operand_blocks(operand, cwd, dirs, files)
                if reason:
                    return _block(reason)
                if "$" in os.path.expandvars(operand):
                    # An UNRESOLVED shell variable target (rm "$TARGET" where
                    # $TARGET is a shell-local, not an env var): expandvars can't
                    # see the assignment, and the real path is usually set in
                    # ANOTHER segment (TARGET=~/genesis/data; rm "$TARGET"). Check
                    # the WHOLE command. Narrow trigger — a resolvable $HOME/… is
                    # already expanded (no `$` left) and handled by _operand_blocks,
                    # so this neither fires on env vars nor resurrects the
                    # mention-only FP (which has no opaque-$var operand).
                    reason = _legacy_substring_block(cmd, dirs)
                    if reason:
                        return _block(reason)
                elif _expand(operand, cwd) is None:
                    seg_unresolved = True
        if seg_unresolved:
            # A relative rm target with no resolvable base: substring-check THIS
            # SEGMENT's raw text (not the whole command — that would resurrect
            # the mention-only false positive this rewrite kills).
            reason = _legacy_substring_block(seg.raw, dirs)
            if reason:
                return _block(reason)

    return 0


if __name__ == "__main__":
    run_guard(main, "protected_paths_guard")
