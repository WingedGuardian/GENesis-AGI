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
import shlex
import sys
from fnmatch import fnmatch

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import brace_expand, read_payload, run_guard, tool_input  # noqa: E402
from shell_parse import analyze  # noqa: E402

try:  # A refusal discards the WHOLE Bash call, so name any write it took with it.
    from discarded_write import remember as _remember_command  # noqa: E402
    from discarded_write import warn as _warn_discarded  # noqa: E402
except Exception:  # noqa: BLE001 — see below

    def _remember_command(_command=None):
        """No-op stand-in.

        The note is cosmetic, but an UNGUARDED import that failed would abort this
        module's load — and CC reads a non-2 exit as a NON-blocking error, so the
        protected-path deletion this hook exists to refuse would proceed. A missing
        note must never become a missing block.
        """

    def _warn_discarded(_command=None):
        """No-op stand-in. See ``_remember_command``."""


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
    # Last, so it reads as a footnote to the refusal above. The command was handed
    # over in main(); stdin was consumed by the payload read and cannot be re-read.
    _warn_discarded()
    return 2


def main() -> int:
    payload = read_payload()
    cmd = tool_input(payload).get("command", "")
    if not cmd or not isinstance(cmd, str):
        return 0
    _remember_command(cmd)

    # Fast path: no rm/rmdir word anywhere in the command.
    if not _RM_PATTERN.search(cmd):
        return 0

    dirs = _protected_dirs()
    files = _protected_files()

    # Explicit tokenizability probe: shell_parse._argv silently degrades to a
    # naive split on shlex errors, so analyze() alone can never signal one.
    try:
        shlex.split(cmd.replace("\\\n", " "))
    except ValueError:
        reason = _legacy_substring_block(cmd, dirs)
        return _block(reason) if reason else 0

    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    for seg in analyze(cmd):
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
