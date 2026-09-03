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

Fail modes: a command this module cannot fully read falls back to the legacy
substring check — conservative, never weaker than the old guard. There are two
such blind spots and `shell_parse.analyze_checked` reports either: a command
shlex cannot tokenize (ANSI-C quoting), and one nested deeper than the parser
follows. The second is a SECURITY bound, not a nicety — an unbounded parse of a
deeply-nested command runs this guard past its registered 10s timeout, and a
hook killed at its timeout does not block, it PERMITS. An unexpected crash fails
CLOSED via hook_input.run_guard (exit 2).
"""

from __future__ import annotations

import os
import re
import sys
from fnmatch import fnmatch

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import brace_expand, read_payload, run_guard, tool_input  # noqa: E402
from shell_parse import analyze_checked  # noqa: E402

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


def _legacy_substring_block(cmd: str, dirs: list[str], because: str) -> str | None:
    """The pre-2026-08 substring check — kept ONLY as the fallback for a command whose
    real target this module cannot pin down (conservative: over-blocks, never under).

    `because` names the situation, and the CALLER owns it because the three that
    reach here are genuinely different: the parse went blind (see
    `shell_parse.analyze_checked` — either shlex could not tokenize the command, or it
    nested deeper than the parser follows); or the parse succeeded and an operand is
    an unresolved shell variable; or a relative operand has no resolvable base.

    The last two are not parse failures at all. This message used to call every one of
    them "an untokenizable rm command" — accurate for one caller of three, and for the
    other two it sent the reader off to fix quoting that was never the problem.
    """
    home = os.path.expanduser("~")
    for prot in dirs:
        for alias in (prot, prot.replace(home, "~", 1), prot.replace(home, "$HOME", 1)):
            if alias in cmd:
                return f"protected path {alias} appears in a command {because}"
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

    # ONE call answers both "what does this run" and "could I read all of it".
    # shell_parse._argv silently degrades to a naive split on shlex errors and the
    # descent stops at a depth bound, so `analyze()` alone can never signal either —
    # a truncated parse and a clean one are the same empty list. Asking through
    # analyze_checked also means a blind spot found LATER is wired in one place
    # rather than in every guard that has to remember to ask about it.
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
    segs, blind = analyze_checked(cmd)
    if blind is not None:
        # A parse stopped by one of shell_parse's BOUNDS blocks OUTRIGHT. It must not
        # fall through to the substring check, because that check is STRICTLY WEAKER
        # than the parse it replaces, and the gap is exactly where the worst commands
        # live. MEASURED at depth 9 — base refuses all three; this guard allowed the
        # first two before this branch existed:
        #     rm -rf $HOME/genesis       (ANCESTOR of a protected dir)  -> ALLOWED
        #     rm -rf $HOME/genesis/*     (GLOB over its contents)       -> ALLOWED
        #     rm -rf $HOME/genesis/data  (the exact path)               -> refused
        # The parse catches the first two via `prot.startswith(expanded + "/")` and
        # via fnmatch; a substring test catches NEITHER, because a protected path is
        # not a substring of a command naming its PARENT. The fallback is therefore
        # at its weakest precisely where the command is most destructive — and the
        # acceptance test that missed this used the one shape the substring test
        # does catch.
        #
        # We are past the _RM_PATTERN fast path, so this can only ever refuse a
        # command that mentions rm, and bounds-induced blindness fires on 0 of 45,358
        # real commands — so refusing outright costs nothing measurable.
        #
        # Both bounds refuse, uniformly with every other fail-closed guard here.
        # The known cost is real and accepted — a here-doc above the cap whose PROSE
        # quotes an `rm -rf` (a review note like this one) is refused rather than
        # scanned. `blind.hint` says to write the payload to a file, which is the
        # action that shape wants anyway.
        if blind.bounds_induced:
            return _block(
                f"an rm command that {blind.cause}, so its real targets cannot be "
                f"resolved. To proceed: {blind.hint}"
            )
        # `untokenizable` keeps the pre-existing substring fallback, unchanged. It is
        # not a bound this change introduced, and widening it here would newly refuse
        # ordinary work under cover of a regression fix.
        reason = _legacy_substring_block(cmd, dirs, f"that {blind.cause}. To proceed: {blind.hint}")
        return _block(reason) if reason else 0

    cwd = payload.get("cwd") if isinstance(payload, dict) else None
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
                    reason = _legacy_substring_block(
                        cmd, dirs, "whose rm target is an unresolved shell variable"
                    )
                    if reason:
                        return _block(reason)
                elif _expand(operand, cwd) is None:
                    seg_unresolved = True
        if seg_unresolved:
            # A relative rm target with no resolvable base: substring-check THIS
            # SEGMENT's raw text (not the whole command — that would resurrect
            # the mention-only false positive this rewrite kills).
            reason = _legacy_substring_block(
                seg.raw, dirs, "whose relative rm target has no resolvable base"
            )
            if reason:
                return _block(reason)

    return 0


if __name__ == "__main__":
    run_guard(main, "protected_paths_guard")
