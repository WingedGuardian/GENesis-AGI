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

- ``~/.claude`` (and ``$CLAUDE_HOME`` when set): ``projects/*/memory``, ``plans``,
  ``CLAUDE.md``, ``settings*.json``, ``skills``, ``agents``, ``commands``,
  ``hookify``;
- ``$GENESIS_HOME`` (default ``~/.genesis``): ``config``, ``output``,
  ``knowledge``, ``uploads``, ``skill-library``, ``plans``, ``eval``,
  ``voice-transcripts``, ``infrastructure``, ``guardian_remote.yaml``,
  ``ambient_remote.yaml``;
- wherever the path overrides Genesis honours point, when set in the hook's
  environment: ``GENESIS_PLANS_DIR``, ``GENESIS_OUTPUT_DIR``,
  ``GENESIS_VOICE_TRANSCRIPT_DIR``, ``SECRETS_PATH`` (a relative value is read from
  the repository root, ``GENESIS_REPO_ROOT`` or this checkout, as the services
  resolve it from their working directory there);
- a location above that is itself a symlink matches through either name: the
  link's path and the directory it points to.
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
  command holding a heredoc (``<<``, not the ``<<<`` here-string: the parser reads
  a heredoc's body lines as commands; a ``<<`` inside quotes silences it too);
- an operand that is a symlink, for ``rm``/``unlink`` without a trailing slash
  (removing the link loses nothing; ``shred`` and ``rm -r link/`` are judged by
  the target they reach), a directory without ``-r`` (the verbs refuse it), an
  empty operand, or past the work bounds (``_MAX_MATCHES``, ``_MAX_VISITS``,
  ``_BUDGET_S``), which also bound the one lookup the user-data set needs (whether
  a directory being removed holds a ``projects/*/memory`` and the like);
- a ``shred`` that deletes nothing: an unknown or ambiguous option, a value given
  to a flag, or ``--help``/``--version`` (shred refuses, or prints and exits);
- an operand holding ``$VAR`` or a backtick (only ``~``, ``$HOME`` and ``${HOME}``
  are expanded: the hook's environment is not the shell's);
- a relative operand after a ``cd`` that is not bare (HOME) or one literal absolute
  or ``~`` path (a ``cd`` to a path that is not a directory when the hook runs is
  read as failing, so the old directory stays: right for ``cd X; rm`` and
  ``cd X || rm``, and for ``cd X && rm`` the note's "if this ran" holds; a
  directory an earlier ``mkdir`` in the same command names counts as existing,
  one made any other way, such as ``git clone``, reads as missing), after any ``cd`` in
  a command with ``(`` grouping or a ``|`` pipe
  (not ``||``; a quoted ``|`` counts too) (the parser
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
_HEREDOC = re.compile(r"(?<!<)<<(?!<)")  # not a <<< here-string, which has no body
_PIPE = re.compile(r"(?<!\|)\|(?!\|)")  # a pipe, not the || list operator
# shred options that take their value as the NEXT word (shred --help, coreutils);
# the long forms also accept --opt=VALUE, which is one word. rm and unlink take
# no separate values.
_SHRED_VALUE_SHORT = frozenset("ns")
_SHRED_SHORT = frozenset("fnsuvxz")  # coreutils 9.4 `shred --help`
# Every long option shred accepts (coreutils 9.4 `shred --help`, read 2026-10-08),
# mapped to whether it takes a REQUIRED value, which getopt then reads from the
# next word. ``--remove[=HOW]``'s value is optional, so only ``=`` supplies it.
# getopt_long also accepts any unambiguous prefix (``--random-sour``), matched here
# against this closed table rather than by name.
_SHRED_LONG = {
    "--exact": False,
    "--force": False,
    "--help": False,
    "--iterations": True,
    "--random-source": True,
    "--remove": False,
    "--size": True,
    "--verbose": False,
    "--version": False,
    "--zero": False,
}
# Override variables the Genesis path resolvers honour (src/genesis/env.py:
# claude_home, plans_dir, output_dir, voice_transcript_dir, secrets_path).
_PATH_OVERRIDES = (
    "GENESIS_PLANS_DIR",
    "GENESIS_OUTPUT_DIR",
    "GENESIS_VOICE_TRANSCRIPT_DIR",
    "SECRETS_PATH",
)

# Every command waits for its PreToolUse hooks (this one is wired at a 10s
# timeout), so the work must stay small whatever the operands expand to. MEASURED:
# `rm -rf ~/tmp/*/*` (72,827 matches) took 16.5s before these bounds, and a
# brace-times-glob operand took minutes. Matching cost about 0.23ms a path there,
# so 256 per operand is about 60ms, and real user-data deletes in 30 days of
# transcripts name at most a few paths. The 2s budget caps the whole command, well
# under the timeout. Past either bound the hook stops reading and notes only what
# it already found: a missed note costs a sentence, a slow hook costs every
# command. Glob expansion is done here, one directory at a time, so the budget
# also bounds a pattern that matches nothing: `glob.iglob` lists every directory
# a multi-level pattern reaches before yielding anything (MEASURED 5.2s for
# `rm -rf ~/tmp/*/*/*.nomatch`), and _MAX_VISITS caps the entries read.
_MAX_MATCHES = 256
_MAX_VISITS = 50_000
_BUDGET_S = 2.0


class _OverBudget(Exception):
    """The work bound was reached; stop reading and note what was found."""


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


def _repo_root() -> str:
    """Where the services resolve a relative path override from: their working
    directory, the repository root (``GENESIS_REPO_ROOT`` when set, else the
    checkout holding this hook, which the launcher runs from the main checkout).
    A RELATIVE ``GENESIS_REPO_ROOT`` is left as given, as ``env.repo_root()`` leaves
    it, so a relative override under it matches nothing here."""
    configured = os.environ.get("GENESIS_REPO_ROOT")
    if configured:
        return os.path.expanduser(configured)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _target_patterns() -> list[tuple[str, ...]]:
    """Every user-data location as path components, globs allowed (``projects/*``).

    Patterns, never an enumeration: an operand is matched against them, so the
    cost does not grow with how many projects or settings files exist. Each base
    is taken under its own name and its resolved one, and a glob-free target also
    as its resolved path, so a protected directory that is a symlink (an output
    directory on another disk) matches whichever name the operand reaches it by.
    """
    home = os.path.expanduser("~")
    claude_bases = {os.path.join(home, ".claude")}
    if os.environ.get("CLAUDE_HOME"):
        claude_bases.add(os.path.expanduser(os.environ["CLAUDE_HOME"]))
    ghome = os.path.expanduser(os.environ.get("GENESIS_HOME") or "~/.genesis")
    bases = [(base, _CLAUDE_TARGETS) for base in claude_bases]
    bases.append((ghome, _GENESIS_HOME_TARGETS))
    patterns: set[tuple[str, ...]] = set()

    def literal(path: str) -> tuple[str, ...]:
        return tuple(glob.escape(p) for p in _parts(os.path.normpath(path)))

    for base, rels in bases:
        for variant in {os.path.normpath(base), os.path.realpath(base)}:
            for rel in rels:
                patterns.add(literal(variant) + tuple(rel.split("/")))
                if not any(ch in rel for ch in _GLOB_CHARS):
                    patterns.add(literal(os.path.realpath(os.path.join(variant, rel))))
    for var in _PATH_OVERRIDES:
        value = os.path.expanduser(os.environ.get(var) or "")
        if not value:
            continue
        if not os.path.isabs(value):
            value = os.path.join(_repo_root(), value)
        patterns.add(literal(value))
        patterns.add(literal(os.path.realpath(value)))
    return sorted(patterns)


def _component(name: str, pattern: str) -> bool:
    """One path component against one pattern component, by the shell's rules: a
    name with a leading dot matches only a pattern with one (a wildcard never
    reaches it), as ``_existing`` lists."""
    if name.startswith(".") and not pattern.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, pattern)


def _matches(parts: list[str], pattern: tuple[str, ...]) -> bool:
    """``parts`` is the location ``pattern`` names, or lies inside it."""
    return len(parts) >= len(pattern) and all(
        _component(a, b) for a, b in zip(parts, pattern, strict=False)
    )


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
    """The new cwd for a bare ``cd`` (HOME) or one literal absolute or ``~``
    target, else None (unknown)."""
    args = argv[1:]
    if argv[0] == "cd" and not args:
        return os.path.expanduser("~")
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


def _existing(path: str, deadline: float) -> list[str]:
    """The existing paths ``path`` names: itself, or a glob's matches expanded one
    directory level at a time with the budget checked at every directory (the
    shell's rules: ``fnmatch`` per component, a leading dot only by a dot)."""
    if not any(ch in path for ch in _GLOB_CHARS):
        return [path] if os.path.lexists(path) else []
    parts = [p for p in path.split("/") if p]
    level = ["/"]
    visits = 0
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        following: list[str] = []
        for base in level:
            if time.monotonic() > deadline:
                raise _OverBudget
            if not any(ch in part for ch in _GLOB_CHARS):
                candidate = os.path.join(base, part)
                if os.path.lexists(candidate) if last else os.path.isdir(candidate):
                    following.append(candidate)
                continue
            try:
                with os.scandir(base) as entries:
                    for entry in entries:
                        visits += 1
                        if visits > _MAX_VISITS:
                            raise _OverBudget
                        name = entry.name
                        if name.startswith(".") and not part.startswith("."):
                            continue
                        if fnmatch.fnmatchcase(name, part) and (last or entry.is_dir()):
                            following.append(os.path.join(base, name))
            except OSError:
                continue
        level = following[:_MAX_MATCHES] if last else following
    return level


def _hits(
    path: str,
    recursive: bool,
    patterns: list[tuple[str, ...]],
    deadline: float,
    given: str | None = None,
) -> bool:
    """``path`` is what the delete reaches; ``given`` is the operand as the command
    spelled it, when that differs (a trailing-slash link is judged by its target)."""
    if os.path.islink(path):
        return False  # removing a link loses nothing; its target stays
    canon = _canon(path)
    is_dir = os.path.isdir(canon)
    if is_dir and not recursive:
        return False  # rm, unlink and shred refuse a directory without -r
    parts = _parts(canon)
    # Both spellings: resolved (a protected directory reached through a link) and as
    # given (a link at or below a wildcard component, such as projects/<p>, which no
    # resolved-path pattern can name).
    spellings = [parts]
    as_given = _parts(os.path.normpath(given or path))
    if as_given != parts:
        spellings.append(as_given)
    if any(_matches(sp, pattern) for sp in spellings for pattern in patterns):
        return True
    if _checkout_file(parts):
        return True
    if is_dir:
        # A user-data location BELOW this directory: look for it, under the same
        # work bounds as an operand's glob (raises _OverBudget past them).
        for sp in spellings:
            n = len(sp)
            base = glob.escape(os.path.join("/", *sp))
            for pattern in patterns:
                if (
                    len(pattern) > n
                    and _matches(sp, pattern[:n])
                    and _existing(os.path.join(base, *pattern[n:]), deadline)
                ):
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
    grouped = "(" in command or bool(_PIPE.search(command))
    cwd0 = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
    seen_cd = inner_cd = False
    made: set[str] = set()  # directories an earlier top-level mkdir names
    patterns: list[tuple[str, ...]] | None = None
    named: list[str] = []
    deadline = time.monotonic() + _BUDGET_S
    for seg in segments:
        if seg.exe == "mkdir" and seg.depth == 0:
            for word in seg.argv[1:]:
                made_path = _resolve(word, cwd0) if not word.startswith("-") else None
                if made_path:
                    made.add(made_path)
            continue
        if seg.exe in ("cd", "pushd", "popd"):
            if seg.depth == 0:
                new = _cd_target(seg.argv) if seg.exe != "popd" else None
                # A cd to a path that is not a directory fails and the shell stays
                # where it was: observed here, never inferred from the operators.
                # One exception, read from operands not operators: a directory an
                # earlier mkdir in this command names will exist by then.
                if new is None or os.path.isdir(new) or new in made:
                    cwd0 = new
                seen_cd = True
            else:
                inner_cd = True
            continue
        if seg.exe not in _VERBS:
            continue
        base = None if (seg.depth and inner_cd) or (grouped and seen_cd) else cwd0
        recursive = seg.exe == "rm" and _recursive(seg.argv)
        operands = _shred_operands(seg.argv) if seg.exe == "shred" else _rm_operands(seg.argv)
        for operand in operands:
            try:
                words = brace_expand(operand)
            except ValueError:
                continue  # a brace bomb: skip this operand, keep reading the others
            for word in words:
                path = _resolve(word, base) if word else None
                if path is None:
                    continue
                # shred writes through a symlink, and `rm -r link/` (trailing slash)
                # descends into its target, so judge those by what they reach.
                follow = seg.exe == "shred" or word.endswith("/")
                try:
                    found_paths = _existing(path, deadline)
                except _OverBudget:
                    return _note(named)
                for found in found_paths:
                    if time.monotonic() > deadline:
                        return _note(named)
                    if patterns is None:
                        patterns = _target_patterns()
                    try:
                        hit = _hits(
                            os.path.realpath(found) if follow else found,
                            recursive,
                            patterns,
                            deadline,
                            given=found,
                        )
                    except _OverBudget:
                        return _note(named)
                    if hit:
                        if operand not in named:
                            named.append(operand)
                        break
    return _note(named)


def _shred_operands(argv: list[str]) -> list[str]:
    """shred's file operands: its value-taking options consume the next word."""
    operands: list[str] = []
    take_value = flags_done = False
    for tok in argv[1:]:
        if take_value:
            take_value = False
            continue
        if not flags_done and tok == "--":
            flags_done = True
            continue
        if not flags_done and tok.startswith("--") and len(tok) > 2:
            name = tok.split("=", 1)[0]
            matched = [o for o in _SHRED_LONG if o == name] or [
                o for o in _SHRED_LONG if o.startswith(name)
            ]
            if len(matched) != 1:
                return []  # unknown or ambiguous: shred refuses the whole command
            option = matched[0]
            # --help/--version print and exit; a value on a flag is refused (--remove's
            # optional value is the one a flag may carry).
            if option in ("--help", "--version") or (
                "=" in tok and not _SHRED_LONG[option] and option != "--remove"
            ):
                return []
            take_value = "=" not in tok and _SHRED_LONG[option]
            continue
        if not flags_done and tok.startswith("-") and len(tok) > 1:
            if tok[1] not in _SHRED_SHORT:
                return []  # an unknown short option: shred refuses the whole command
            for i, ch in enumerate(tok[1:], start=1):
                if ch not in _SHRED_SHORT:
                    return []
                if ch in _SHRED_VALUE_SHORT:
                    take_value = i == len(tok) - 1  # -n3 carries its value; -n does not
                    break
            continue
        operands.append(tok)
    return operands


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
