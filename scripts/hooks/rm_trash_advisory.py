#!/usr/bin/env python3
"""Advisory: a shell delete of user data, which the Genesis trash could have kept.

ADVISORY ONLY. It never blocks and never asks: it adds one note to the session's
context and always exits 0, so a dispatched session is affected exactly as a
foreground one is (it sees the note).

WHAT IT IS FOR (#2926, PR 6b)
=============================
Deleting user or project data goes through ``python -m genesis.trash put`` so the
delete can be undone; ``rm`` is for a session's own files and temp. That rule lives
in the dev skill and CLAUDE.md, and this hook is where it is met at the moment of
the command. It fires on ``rm``, ``unlink`` and ``shred`` when an operand is, or
(for ``rm -r``) contains, an EXISTING path in a user-data location:

- ``~/.claude``: ``projects/*/memory``, ``plans``, ``CLAUDE.md``,
  ``settings*.json``, ``skills``, ``agents``, ``commands``, ``hookify``;
- ``$GENESIS_HOME`` (default ``~/.genesis``): ``config``, ``output``,
  ``knowledge``, ``uploads``, ``skill-library``, ``plans``, ``eval``,
  ``voice-transcripts``, ``infrastructure``, ``guardian_remote.yaml``,
  ``ambient_remote.yaml``;
- in any Genesis checkout (a directory holding ``src/genesis``), the gitignored
  files git cannot restore: ``src/genesis/identity/{USER,USER_KNOWLEDGE,
  TRIAGE_CALIBRATION,EGO_NOTEPAD}.md``, ``config/*.local.yaml``,
  ``.claude/settings.local.json`` and ``secrets.env``.

MEASURED before building (30 days of this install's transcripts, 1,038 commands
running ``rm``): this set fires on about 7 commands a month, and 1 of them was user
data, deleted on purpose. The rest were a session's own drafts and probes, which is
why the note is worded for that case too. A PreToolUse note reaches the session
while the command runs, so it shapes the NEXT delete, not this one. It is a
habit-former, not a safeguard; the blocking guards (``protected_paths_guard``,
``destructive_command_guard``) are the safeguards, and run alongside it.

WHAT IT CANNOT SEE (it stays silent, never guesses)
===================================================
- a command the parser reports blind (``shell_parse.analyze_checked``), or any
  command holding a heredoc (``<<``: the parser reads its body lines as commands);
- an operand that is a symlink (removing it loses nothing), a directory without
  ``-r`` (the verbs refuse it), or past the work bounds (``_MAX_MATCHES``,
  ``_BUDGET_S``);
- an operand holding ``$VAR`` or a backtick (only ``~``, ``$HOME`` and ``${HOME}``
  are expanded: the hook's environment is not the shell's);
- a relative operand after a ``cd`` that is not one literal absolute or ``~``
  path, after any ``cd`` in a command with ``(`` grouping or a pipe (the parser
  flattens subshells), or after any ``cd`` inside ``bash -c``; ``env -C`` and
  ``sudo -D`` directory changes are not seen at all;
- ``rm -r`` of a directory holding a whole Genesis checkout below it (a checkout
  itself, or a directory inside one, is checked; ``_checkout_ancestor`` says why);
- ``find -delete``, ``git clean``, ``xargs rm`` (its operands come from stdin),
  ``python -c 'shutil.rmtree(...)'``, and ``cp`` or a redirect over a file. Of
  ``find -delete`` and ``git clean``, 30 days of transcripts held one ``find`` hit,
  a false one, and no ``git clean`` of user data. ``git rm`` is not matched: git
  can restore what it tracked.

It does over-read one thing: shlex strips quotes, so a quoted ``~``, ``$HOME`` or
glob, which the shell leaves literal, is expanded here anyway.
"""

from __future__ import annotations

import fnmatch
import glob
import itertools
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Wrapped: an advisory must never turn a broken helper into a refused command.
# Imported as a module (not __main__), protected_paths_guard RAISES on a broken
# helper rather than exiting 2, so this except sees it.
try:
    from hook_input import brace_expand, read_payload, tool_input
    from hook_output import print_json_bounded
    from protected_paths_guard import _rm_operands
    from shell_parse import analyze_checked

    _IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # noqa: BLE001 - advisory: degrade to silence
    _IMPORT_ERROR = _exc

_VERBS = frozenset({"rm", "unlink", "shred"})
_PREFILTER = re.compile(r"\b(?:rm|unlink|shred)\b")
_GLOB_CHARS = ("*", "?", "[")
_HEREDOC = re.compile(r"<<(?!<)")

# Every command waits for its PreToolUse hooks (this one is wired at a 10s
# timeout), so the work must stay small whatever the operands expand to. MEASURED:
# `rm -rf ~/tmp/*/*` (72,827 matches) took 16.5s before these bounds, and a
# brace-times-glob operand took minutes. Matching cost about 0.23ms a path there,
# so 256 per operand is about 60ms, and real user-data deletes in 30 days of
# transcripts name at most a few paths. The 2s budget caps the whole command, well
# under the timeout. Past either bound the hook stops reading and notes only what
# it already found: a missed note costs a sentence, a slow hook costs every
# command.
_MAX_MATCHES = 256
_BUDGET_S = 2.0

_CLAUDE_TARGETS = (
    "projects/*/memory",
    "plans",
    "CLAUDE.md",
    "settings*.json",
    "skills",
    "agents",
    "commands",
    "hookify",
)
_GENESIS_HOME_TARGETS = (
    "config",
    "output",
    "knowledge",
    "uploads",
    "skill-library",
    "plans",
    "eval",
    "voice-transcripts",
    "infrastructure",
    "guardian_remote.yaml",
    "ambient_remote.yaml",
)
# Relative to a Genesis checkout root (a directory holding src/genesis).
_CHECKOUT_TARGETS = tuple(
    tuple(p.split("/"))
    for p in (
        "src/genesis/identity/USER.md",
        "src/genesis/identity/USER_KNOWLEDGE.md",
        "src/genesis/identity/TRIAGE_CALIBRATION.md",
        "src/genesis/identity/EGO_NOTEPAD.md",
        "config/*.local.yaml",
        ".claude/settings.local.json",
        "secrets.env",
    )
)

_NOTE = (
    "If this ran, it permanently deleted {paths}: user or project data that git "
    "cannot restore. If this session created them itself, nothing to do. "
    "Otherwise, next time delete user or project data with "
    "`~/genesis/.venv/bin/python -m genesis.trash put PATH --reason R`, which can "
    "be undone (if put refuses, ask the user; do not fall back to rm), and if this "
    "delete was not intended, say so now (a dispatched session: in its report)."
)


def _canon(path: str) -> str:
    """The path with its PARENT resolved: a symlink operand stays the link."""
    parent, name = os.path.split(os.path.normpath(path))
    return os.path.join(os.path.realpath(parent or "."), name)


def _home_targets() -> list[str]:
    home = os.path.expanduser("~")
    ghome = os.path.expanduser(os.environ.get("GENESIS_HOME") or "~/.genesis")
    found: list[str] = []
    for base, rels in (
        (os.path.join(home, ".claude"), _CLAUDE_TARGETS),
        (ghome, _GENESIS_HOME_TARGETS),
    ):
        for rel in rels:
            found.extend(_canon(p) for p in glob.glob(os.path.join(base, rel)))
    return found


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _is_checkout(root_parts: list[str]) -> bool:
    return os.path.isdir(os.path.join("/", *root_parts, "src", "genesis"))


def _checkout_file(parts: list[str]) -> bool:
    """``parts`` IS one of the checkout targets, inside a Genesis checkout."""
    for target in _CHECKOUT_TARGETS:
        n = len(target)
        tail = zip(parts[-n:], target, strict=False)
        if (
            len(parts) > n
            and all(fnmatch.fnmatchcase(a, b) for a, b in tail)
            and _is_checkout(parts[:-n])
        ):
            return True
    return False


def _checkout_ancestor(parts: list[str]) -> bool:
    """The directory ``parts`` is a checkout, or inside one, and holds a checkout target.

    Only stats, never a listing of ``parts`` itself: a wildcard over a directory's
    children made ``rm -rf ~/tmp/*`` (about 3,000 entries) cost 45 seconds of
    globbing in this hook, past its 10-second timeout, and every command waits for
    its hooks. So a directory holding a whole checkout one level DOWN is not seen.
    """
    path = os.path.join("/", *parts)
    for target in _CHECKOUT_TARGETS:
        for k in range(len(target)):
            if k and (len(parts) <= k or parts[-k:] != list(target[:k])):
                continue
            root = parts[: len(parts) - k]
            if _is_checkout(root) and glob.glob(os.path.join(path, *target[k:])):
                return True
    return False


def _recursive(argv: list[str]) -> bool:
    for tok in argv[1:]:
        if tok == "--":
            return False
        if tok == "--recursive" or (
            re.fullmatch(r"-[A-Za-z]+", tok) and ("r" in tok or "R" in tok)
        ):
            return True
    return False


def _cd_target(argv: list[str]) -> str | None:
    """The new cwd for one literal absolute or ``~`` target, else None (unknown)."""
    args = argv[1:]
    if len(args) != 1:
        return None
    tok = args[0]
    if tok == "~" or tok.startswith("~/"):
        tok = os.path.expanduser(tok)
    if "$" in tok or "`" in tok or not os.path.isabs(tok):
        return None
    return os.path.normpath(tok)


def _resolve(word: str, base: str | None) -> str | None:
    home = os.path.expanduser("~")
    for prefix in ("$HOME", "${HOME}"):
        if word == prefix or word.startswith(prefix + "/"):
            word = home + word[len(prefix) :]
    if word == "~" or word.startswith("~/"):
        word = os.path.expanduser(word)
    if "$" in word or "`" in word:
        return None
    if not os.path.isabs(word):
        if base is None:
            return None
        word = os.path.join(base, word)
    return os.path.normpath(word)


def _existing(path: str) -> list[str]:
    if any(ch in path for ch in _GLOB_CHARS):
        return list(itertools.islice(glob.iglob(path), _MAX_MATCHES))
    return [path] if os.path.lexists(path) else []


def _hits(path: str, recursive: bool, targets: list[str]) -> bool:
    if os.path.islink(path):
        return False  # removing a link loses nothing; its target stays
    canon = _canon(path)
    is_dir = os.path.isdir(canon)
    if is_dir and not recursive:
        return False  # rm, unlink and shred refuse a directory without -r
    if any(canon == t or canon.startswith(t + "/") for t in targets):
        return True
    parts = _parts(canon)
    if _checkout_file(parts):
        return True
    if is_dir:
        prefix = canon.rstrip("/") + "/"
        if any(t.startswith(prefix) for t in targets):
            return True
        return _checkout_ancestor(parts)
    return False


def _advisory(command: str, cwd: str | None) -> str | None:
    if not _PREFILTER.search(command):
        return None
    if _HEREDOC.search(command):
        # The parser has no heredoc state, so a body line reads as a command: a
        # script or PR body being written that mentions rm would get a note about a
        # delete that never ran.
        return None
    segments, blind = analyze_checked(command)
    if blind is not None:
        return None
    # A cd inside ( ) or a pipeline runs in a subshell, which the flat segment list
    # cannot show, so after any cd in such a command relative paths are unknown.
    grouped = "(" in command or "|" in command
    cwd0 = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
    seen_cd = inner_cd = False
    targets: list[str] | None = None
    named: list[str] = []
    deadline = time.monotonic() + _BUDGET_S
    for seg in segments:
        if seg.exe in ("cd", "pushd", "popd"):
            if seg.depth == 0:
                cwd0 = _cd_target(seg.argv) if seg.exe != "popd" else None
                seen_cd = True
            else:
                inner_cd = True
            continue
        if seg.exe not in _VERBS:
            continue
        base = None if (seg.depth and inner_cd) or (grouped and seen_cd) else cwd0
        recursive = seg.exe == "rm" and _recursive(seg.argv)
        for operand in _rm_operands(seg.argv):
            try:
                words = brace_expand(operand)
            except ValueError:
                continue  # a brace bomb: skip this operand, keep reading the others
            for word in words:
                path = _resolve(word, base)
                if path is None:
                    continue
                for found in _existing(path):
                    if time.monotonic() > deadline:
                        return _note(named)
                    if targets is None:
                        targets = _home_targets()
                    if _hits(found, recursive, targets):
                        if operand not in named:
                            named.append(operand)
                        break
    return _note(named)


def _note(named: list[str]) -> str | None:
    if not named:
        return None
    return _NOTE.format(paths=", ".join(f"`{p}`" for p in named))


def main() -> int:
    if _IMPORT_ERROR is not None:
        print(f"rm_trash_advisory: import failed: {_IMPORT_ERROR!r}", file=sys.stderr)
        return 0
    try:
        payload = read_payload()
        command = (tool_input(payload) or {}).get("command")
        if not isinstance(command, str) or not command:
            return 0
        note = _advisory(command, payload.get("cwd"))
        if note:
            print_json_bounded(
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}},
                text_keys=("hookSpecificOutput.additionalContext",),
            )
    except Exception as exc:  # noqa: BLE001 - advisory: never interfere with a command
        print(f"rm_trash_advisory: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
