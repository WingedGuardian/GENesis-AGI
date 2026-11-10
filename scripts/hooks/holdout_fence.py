#!/usr/bin/env python3
"""Advisory fence around an install's hidden holdout scenarios.

ADVISORY ONLY. It never blocks, never asks and always exits 0, so a dispatched
session is affected exactly as a foreground one is: it sees the note.

WHAT IT IS FOR
==============
A holdout (``docs/reference/holdout.md``) is a set of exact checks that builders
never see, kept in ``$GENESIS_HOME/holdout/`` (default ``~/.genesis/holdout``).
Only the ``holdout-writer`` agent may read or write it; validators use
``scripts/holdout_runner.py``, which prints ids and verdicts only. A session that
opens the file spoils the scenarios it reads, and nothing would show it. This
hook records every such access, so the record shows whether the set is still
hidden, and tells the agent to stop. The runner prints the count of recorded
exposures on every run; the record is also the measured basis for any later
decision to block, which this hook does not make.

Two events, one file:

- ``pre`` (PreToolUse). A path OPERAND that reaches the holdout gets a note and a
  ``read``, ``write`` or ``access`` row: a file tool's path, a Glob pattern
  (joined to its ``path``), a Grep ``path`` plus ``glob``, a shell redirect, or an
  argument of a shell command that reads files. Globs and braces are matched as
  strings against the holdout's path; nothing is expanded on disk, so a pattern
  over a large tree costs nothing. A pattern that reaches the holdout only
  through ``**``, or a recursive shell search of a directory above it, is a
  ``broad`` row with no note: those searches are common, and ripgrep-based ones
  skip the directory because the runner keeps ``holdout/`` in
  ``$GENESIS_HOME/.ignore``. A path that is only MENTIONED (a brief, a message,
  a search pattern, ``echo``, ``gh``, ``git``) is a ``mention`` row with no note.
- ``post`` (PostToolUse). A result containing a line of a scenario's expected
  output (whitespace collapsed, 40 characters or longer), or the holdout
  directory's path as a whole component, gets a note telling the agent not to
  use, repeat or store it, and a ``content`` row. This catches what operand
  matching cannot: a copy made elsewhere, a shell variable, a recall result.
  Steps are not fingerprinted, because they exercise repository code and share
  lines with it. The runner's own output, and reads of the exposure log, are
  skipped.

The ``holdout-writer`` agent (a subagent of that type, or a main session started
as it) is allowed: its accesses are ``allowed`` rows naming only the tool and
the path, never a command (which could carry scenario text), and get no note.

Two logs under ``$GENESIS_HOME/logs/``, one JSON object per line: ``at``,
``event``, ``kind``, ``session_id``, ``agent_id``, ``agent_type``, ``tool``, and
``detail`` (a path or command, at most 200 characters; for ``content``, only the
ids of the scenarios matched, never their text).

- ``holdout-exposures.jsonl``: ``read``, ``write``, ``access`` and ``content``,
  the rows that mean the set may be spoiled, and ``allowed``. Never rotated, so
  no exposure is ever lost; the runner counts it on every run, writer accesses
  separately.
- ``holdout-fence-activity.jsonl``: ``mention``, ``broad`` and ``listing``.
  Rotated to ``.1`` at 5 MiB.

The writer is identified only by the ``agent_type`` an agent declares, so any
session can claim it. That is why its rows are kept and counted, not discarded:
the owner matches them against the writer runs they ordered. This fence catches
honest mistakes; it cannot prove the set was not read on purpose, and "no
exposures" is not that proof.

Each is appended under a lock on a separate ``.lock`` file, so rotation (which
replaces the log) can never leave a second writer locking the old inode.

COST
====
The wiring checks for the holdout directory in the shell before starting this
script, so an install without one starts no Python at all. With one, ``pre``
scans the tool input and ``post`` the first 2 MiB of the result, with no disk
walk in either.

WHAT IT CANNOT SEE
==================
- a path built at run time (``cat "$D/HOLDOUT.md"`` with ``D`` set earlier, an
  interpreter that opens it), until its content shows up in a result;
- an expected-output line under 40 characters, or content past the first 2 MiB;
- clients other than Claude Code and Codex CLI, and any process outside an agent
  session.

Fail-open on anything unexpected: an advisory must never turn a crash into a
refused tool call.
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import re
import sys

ALLOWED_AGENT_TYPES = frozenset({"holdout-writer"})
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
RESULT_SCAN_BYTES = 2 * 1024 * 1024
MIN_FINGERPRINT_CHARS = 40
LOG_ROTATE_BYTES = 5 * 1024 * 1024
DETAIL_CHARS = 200
EXPANSION_LIMIT = 64  # brace alternatives per pattern
SCENARIO_FILE = "HOLDOUT.md"

#: Shell commands whose operands are NOT read: a path among their arguments is a
#: mention. Every other command's operand that reaches the holdout is an access.
MENTION_VERBS = frozenset(
    {"echo", "printf", "gh", "true", ":", "test", "[", "export", "alias", "type", "which"}
)
#: Commands that show names or metadata, never contents: a "listing", recorded
#: in the activity log with no note.
LISTING_VERBS = frozenset(
    {
        "ls", "du", "stat", "file", "tree", "readlink", "realpath", "find", "fd",
        "basename", "dirname",
    }
)  # fmt: skip
#: Searchers: their first positional is a PATTERN (a mention), the rest are paths.
SEARCHERS = frozenset({"rg", "grep", "egrep", "fgrep", "ag", "ack"})
#: Options whose VALUE is the next word, per command family: a pattern (a
#: mention), a file read for patterns (an operand), or anything else (dropped).
#: From `rg --help` and `grep --help`; the "=value" spellings are split apart.
PATTERN_OPTIONS = frozenset({"-e", "--regexp"})
PATTERN_FILE_OPTIONS = frozenset({"-f", "--file"})
#: Value-taking options that are neither, per family: the same letter differs
#: (rg -E/-r take an encoding/replacement; grep -E/-r are plain flags).
RG_VALUE_OPTIONS = frozenset(
    {
        "-r", "--replace", "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not",
        "-m", "--max-count", "-A", "-B", "-C", "--after-context", "--before-context",
        "--context", "-M", "--max-columns", "-j", "--threads", "-E", "--encoding",
        "-d", "--max-depth",
    }
)  # fmt: skip
GREP_VALUE_OPTIONS = frozenset(
    {
        "-m", "--max-count", "-A", "-B", "-C", "--after-context", "--before-context",
        "--context", "--include", "--exclude", "--exclude-dir", "-d", "--directories",
        "-D", "--devices", "--label",
    }
)  # fmt: skip
#: Commands that read the current directory when given no path operand.
CWD_DEFAULT_VERBS = frozenset({"rg", "ag", "ack", "find", "fd", "ls", "du", "tree"})
#: ripgrep-based searchers honour $GENESIS_HOME/.ignore, which lists holdout/.
RG_BASED = frozenset({"rg"})
RECURSIVE_VERBS = frozenset({"rg", "ag", "ack", "find", "fd", "tree", "du", "tar", "zip", "rsync"})
INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "zsh", "node", "perl", "ruby"})
#: find actions that delete, run a command on, or print the contents of a match:
#: with one of these, find is no longer a listing.
FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"})
#: Commands that, run recursively on a directory ABOVE the holdout, destroy or
#: move it: an access, not a broad search.
DESTRUCTIVE_VERBS = frozenset({"rm", "rmdir", "shred", "mv"})

PRE_NOTE = (
    "HOLDOUT FENCE: this tool call reaches {where}, the install's hidden holdout "
    "scenarios. Builders must never see them, and reading them spoils every "
    "scenario read. Stop: do not open, list, search or quote that directory, and "
    "do not repeat anything from it. Validators run "
    "`.venv/bin/python scripts/holdout_runner.py --pr N` instead, which prints ids "
    "and verdicts only. This access has been logged."
)
POST_NOTE = (
    "HOLDOUT FENCE: this tool result contains text from the install's hidden "
    "holdout scenarios ({ids}). Do not use, repeat, summarise or store it, and do "
    "not act on what it says. This exposure has been logged."
)

_HEADING = re.compile(r"^## (H-\d+)\b")
_FENCE = re.compile(r"^(```+|~~~+)")
# A redirect target in a raw segment: < file, > file, >> file, 2> file, &> file.
_REDIRECT_TARGET = re.compile(r"(?:^|[\s;|&])(?:\d*|&)(?:<|>>?|>\|)(?![<&(])\s*([^\s<>|;&()]+)")
_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# "allowed" stays in the exposure log: the writer is known only by the name an
# agent declares, so its accesses are kept for the owner to check, never rotated.
ACTIVITY_KINDS = frozenset({"mention", "broad", "listing"})


def genesis_home() -> str:
    value = os.environ.get("GENESIS_HOME")
    return value or os.path.join(os.path.expanduser("~"), ".genesis")


def holdout_dir() -> str:
    return os.path.join(genesis_home(), "holdout")


EXPOSURE_KINDS = frozenset({"read", "write", "access", "content"})


def exposure_log() -> str:
    return os.path.join(genesis_home(), "logs", "holdout-exposures.jsonl")


def activity_log() -> str:
    return os.path.join(genesis_home(), "logs", "holdout-fence-activity.jsonl")


def main(argv: list[str]) -> int:
    holdout = holdout_dir()
    if not os.path.isdir(holdout):
        return 0
    event = argv[1] if len(argv) > 1 else ""
    if event not in ("pre", "post"):
        return 0
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from hook_input import read_payload

        payload = read_payload()
        if not isinstance(payload, dict) or not payload:
            return 0
        note = _pre(payload, holdout) if event == "pre" else _post(payload, holdout)
        if note:
            _emit("PreToolUse" if event == "pre" else "PostToolUse", note)
    except Exception:  # noqa: BLE001 - advisory: never fail a tool call
        return 0
    return 0


# ---------------------------------------------------------------- matching


def _abs(path: str, base: str) -> str:
    # The hook may run without GENESIS_HOME exported; the spelling still means it.
    for form in ("${GENESIS_HOME}", "$GENESIS_HOME"):
        if path.startswith(form):
            path = genesis_home() + path[len(form) :]
    expanded = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return os.path.normpath(expanded)


def _braces(pattern: str) -> list[str]:
    """Expand ``{a,b}`` alternatives (bounded), the way a shell or ripgrep would."""
    out, todo = [], [pattern]
    while todo and len(out) + len(todo) <= EXPANSION_LIMIT:
        p = todo.pop()
        m = re.search(r"\{([^{}]*,[^{}]*)\}", p)
        if not m:
            out.append(p)
            continue
        todo.extend(p[: m.start()] + alt + p[m.end() :] for alt in m.group(1).split(","))
    return out + todo


def _segments_reach(pattern: list[str], target: list[str]) -> tuple[bool, bool]:
    """Whether a glob, as path segments, can name the target or something inside it.

    Returns ``(reaches, only_via_doublestar)``. ``**`` matches any number of
    segments. A pattern that ends above the target names only an ancestor.
    """
    best: tuple[bool, bool] = (False, False)
    stack = [(0, 0, False)]
    seen = set()
    while stack:
        i, j, star = stack.pop()
        if (i, j, star) in seen:
            continue
        seen.add((i, j, star))
        if j == len(target):
            if not star:
                return True, False
            best = (True, True)
            continue
        if i == len(pattern):
            continue
        seg = pattern[i]
        hidden = target[j].startswith(".")
        if seg == "**":
            stack.append((i + 1, j, True))
            # Like bash and ripgrep, "**" never descends into a hidden directory.
            if not hidden:
                stack.append((i, j + 1, True))
        elif fnmatch.fnmatchcase(target[j], seg) and (not hidden or seg.startswith(".")):
            # A wildcard segment matches a dot-name only when it starts with ".".
            stack.append((i + 1, j + 1, star))
    return best


def reach(path: str, base: str, root: str) -> str | None:
    """``"exact"``, ``"broad"`` or None: how a path or glob relates to ``root``."""
    kind = None
    for alt in _braces(path):
        absolute = _abs(alt, base)
        segs = [s for s in absolute.split(os.sep) if s]
        rsegs = [s for s in root.split(os.sep) if s]
        hit, via_star = _segments_reach(segs, rsegs)
        if hit and not via_star:
            return "exact"
        # A pattern that can name the scenario file itself lists or reads it,
        # whether or not it got there through "**".
        if _segments_reach(segs, rsegs + [SCENARIO_FILE])[0] and _names_file(segs):
            return "exact"
        if hit:
            kind = "broad"
        elif _is_ancestor(absolute, root):
            kind = kind or "above"
    return kind


def _names_file(segs: list[str]) -> bool:
    """Whether the pattern's last segment can match the scenario file's name."""
    return bool(segs) and segs[-1] != "**" and fnmatch.fnmatchcase(SCENARIO_FILE, segs[-1])


def _is_ancestor(path: str, root: str) -> bool:
    return root != path and root.startswith(path.rstrip(os.sep) + os.sep)


def _roots(holdout: str) -> list[str]:
    roots = {os.path.normpath(holdout)}
    real = os.path.realpath(holdout)
    roots.add(real)
    return sorted(roots)


def _best(path: str, base: str, roots: list[str]) -> str | None:
    kinds = {reach(path, base, r) for r in roots}
    for k in ("exact", "broad", "above"):
        if k in kinds:
            return k
    return None


# ---------------------------------------------------------------- pre


def _pre(payload: dict, holdout: str) -> str | None:
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = str(payload.get("cwd") or os.getcwd())
    roots = _roots(holdout)
    allowed = payload.get("agent_type") in ALLOWED_AGENT_TYPES

    if tool in ("Agent", "Task"):
        if ti.get("subagent_type") in ALLOWED_AGENT_TYPES or allowed:
            return None
        prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
        if _mentions(prompt, roots):
            _log(payload, "pre", "mention", _home_relative(holdout))
        return None

    found = _classify(tool, ti, cwd, roots, holdout)
    if not found:
        return None
    kind, detail = found
    if allowed:
        # Only the directory: a writer's command can carry scenario text.
        _log(payload, "pre", "allowed", _home_relative(holdout))
        return None
    _log(payload, "pre", kind, ti.get("command") or detail)
    return PRE_NOTE.format(where=holdout) if kind in EXPOSURE_KINDS else None


#: How strongly each kind counts, so a call reporting several keeps the worst.
_RANK = {"write": 6, "read": 5, "access": 5, "listing": 3, "broad": 2, "mention": 1}


def _classify(tool: str, ti: dict, cwd: str, roots: list[str], holdout: str):
    """(kind, detail) for the strongest way this call reaches the holdout, or None."""
    hits: list[tuple[str, str]] = []

    def path_hit(path: str, base: str, exact_kind: str, *, recursive: bool, rg: bool = False):
        how = _best(path, base, roots)
        if how == "exact":
            if rg and _rg_ignores(holdout) and _glob_only_reaches_via_parent(path, base, roots):
                hits.append(("broad", path))
            else:
                hits.append((exact_kind, path))
        elif how == "broad" or (
            how == "above" and recursive and not (rg and _crosses_hidden(path, base, roots))
        ):
            hits.append(("broad", path))

    for key in ("file_path", "notebook_path"):
        if isinstance(ti.get(key), str) and ti[key]:
            path_hit(ti[key], cwd, "write" if tool in WRITE_TOOLS else "read", recursive=False)
    path = ti.get("path") if isinstance(ti.get("path"), str) and ti.get("path") else None
    if tool == "Glob":
        base = _abs(path, cwd) if path else cwd
        if isinstance(ti.get("pattern"), str):
            path_hit(ti["pattern"], base, "read", recursive=True)
    elif tool == "Grep":
        base = _abs(path, cwd) if path else cwd
        path_hit(base, cwd, "read", recursive=True, rg=True)
        glob = ti.get("glob")
        if isinstance(glob, str) and glob:
            # A ripgrep glob without a slash matches a basename at any depth.
            path_hit(glob if "/" in glob else f"**/{glob}", base, "read", recursive=True, rg=True)
        if isinstance(ti.get("pattern"), str) and _mentions(ti["pattern"], roots):
            hits.append(("mention", ti["pattern"]))
    command = ti.get("command") if isinstance(ti.get("command"), str) else ""
    if command:
        hits.extend(_shell_hits(command, cwd, roots, holdout))
    if not hits:
        return None
    return max(hits, key=lambda h: _RANK[h[0]])


def _shell_hits(command: str, cwd: str, roots: list[str], holdout: str) -> list[tuple[str, str]]:
    """Classify a shell command with the repository's shell parser."""
    from shell_parse import analyze_checked

    hits: list[tuple[str, str]] = []
    stripped, bodies = _strip_heredocs(command)
    for verb, body in bodies:
        if _mentions(body, roots):
            hits.append(("access" if verb in INTERPRETERS else "mention", command))
    try:
        segments, blind = analyze_checked(stripped)
    except Exception:  # noqa: BLE001 - an unreadable command is judged below
        segments, blind = [], True
    if blind or not segments:
        # Unreadable: report toward the safe side if it names the holdout at all.
        if _mentions(stripped, roots):
            hits.append(("access", command))
        return hits
    # Redirect targets are read off the command text: the parser drops them from
    # each segment's argv and raw text. Relative ones are judged from the cwd the
    # call started in, which a cd earlier in the line can make wrong.
    for target in _REDIRECT_TARGET.findall(stripped):
        if _best(target.strip("'\""), cwd, roots) == "exact":
            hits.append(("access", command))
    base = cwd
    for seg in segments:
        argv = list(seg.argv or [])
        if not argv:
            continue
        verb = os.path.basename(seg.exe or argv[0])
        args = argv[1:]
        if verb == "cd":
            base = _abs(next((a for a in args if not a.startswith("-")), "~"), base)
            continue
        if verb in INTERPRETERS and any(_mentions(a, roots) for a in args):
            hits.append(("access", command))  # python3 -c, perl -e, bash -c text
        positionals, patterns, pattern_files = _split_args(verb, args)
        # A value after "=" is an operand too: dd if=PATH. Option spellings
        # (--file=PATH) were split above.
        positionals += [
            a.split("=", 1)[1]
            for a in args
            if "=" in a and not a.startswith("-") and a.split("=", 1)[1]
        ]
        for pattern in patterns:
            if _mentions(pattern, roots):
                hits.append(("mention", command))
        positionals += pattern_files
        recursive = verb in RECURSIVE_VERBS or _has_recursive_flag(verb, args)
        if verb in MENTION_VERBS or (verb == "git" and "--no-index" not in args):
            if any(_mentions(a, roots) or _best(a, base, roots) == "exact" for a in args):
                hits.append(("mention", command))
            continue
        if verb in SEARCHERS and positionals and not patterns:
            pattern = positionals.pop(0)
            if _mentions(pattern, roots):
                hits.append(("mention", command))
        if not positionals and (verb in CWD_DEFAULT_VERBS or (verb in SEARCHERS and recursive)):
            positionals = ["."]  # it reads the current directory
        lists_only = verb in LISTING_VERBS and not (verb == "find" and FIND_ACTIONS & set(args))
        exact_kind = "listing" if lists_only else "access"
        for operand in positionals:
            how = _best(operand, base, roots)
            if how == "exact":
                if (
                    verb in RG_BASED
                    and _rg_ignores(holdout)
                    and _glob_only_reaches_via_parent(operand, base, roots)
                ):
                    hits.append(("broad", command))
                else:
                    hits.append((exact_kind, command))
            elif (
                how == "above"
                and recursive
                and (verb in DESTRUCTIVE_VERBS or (verb == "find" and "-delete" in args))
            ):
                hits.append(("access", command))  # removing a parent removes the holdout
            elif how == "broad" or (
                how == "above"
                and recursive
                and not (verb in RG_BASED and _crosses_hidden(operand, base, roots))
            ):
                hits.append(("broad", command))
    return hits


def _strip_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
    """The command with heredoc bodies removed, and (verb, body) for each body."""
    lines = command.split("\n")
    out: list[str] = []
    bodies: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _HEREDOC_START.finditer(line):
            if _in_arithmetic(line, m.start()):
                continue  # $(( a << b )) or (( a << b )) is a shift
            word = m.group(2)
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != word:
                body.append(lines[i])
                i += 1
            i += 1  # the terminator
            head = line[: m.start()].split()
            verb = os.path.basename(head[-1]) if head else ""
            for w in reversed(head):
                if os.path.basename(w) in INTERPRETERS:
                    verb = os.path.basename(w)
                    break
            bodies.append((verb, "\n".join(body)))
    return "\n".join(out), bodies


def _in_arithmetic(line: str, index: int) -> bool:
    """Whether ``index`` sits inside an open ``((`` on this line."""
    before = line[:index]
    return before.count("((") > before.count("))")


def _rg_ignores(holdout: str) -> bool:
    """Whether $GENESIS_HOME/.ignore lists the holdout directory for ripgrep."""
    name = os.path.basename(holdout.rstrip(os.sep))
    try:
        with open(os.path.join(os.path.dirname(holdout), ".ignore"), encoding="utf-8") as fh:
            return any(ln.strip().strip("/") == name for ln in fh)
    except (OSError, ValueError):
        return False


def _glob_only_reaches_via_parent(path: str, base: str, roots: list[str]) -> bool:
    """True unless the path names the holdout directory explicitly (ripgrep skips
    an ignored directory it walks into, but searches one it is pointed at)."""
    absolute = _abs(path, base)
    return not any(absolute == r or absolute.startswith(r + os.sep) for r in roots)


def _crosses_hidden(path: str, base: str, roots: list[str]) -> bool:
    """Whether reaching the holdout from ``path`` passes a hidden directory,
    which a ripgrep-based search never walks into unless pointed at it."""
    start = _abs(path, base).rstrip(os.sep)
    for r in roots:
        if r.startswith(start + os.sep):
            below = r[len(start) + 1 :].split(os.sep)
            if any(part.startswith(".") for part in below):
                return True
    return False


def _home_relative(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def _has_recursive_flag(verb: str, args: list[str]) -> bool:
    flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if verb in ("grep", "egrep", "fgrep"):
        return "r" in flags or "R" in flags or "--recursive" in args
    if verb in ("ls", "chmod", "chown"):
        return "R" in flags or "--recursive" in args
    if verb in ("cp", "rm"):
        return any(c in flags for c in "rRa" if verb == "cp" or c != "a") or "--recursive" in args
    return False


def _split_args(verb: str, args: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(path operands, pattern values, pattern-file values) of one command.

    Option values are consumed with their option, so ``rg -e PATTERN docs/``
    leaves ``docs/`` as the only path and ``PATTERN`` as a mention.
    """
    positionals: list[str] = []
    patterns: list[str] = []
    pattern_files: list[str] = []
    takes_values = verb in SEARCHERS
    other_values = RG_VALUE_OPTIONS if verb in ("rg", "ag") else GREP_VALUE_OPTIONS
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positionals.extend(args[i + 1 :])
            break
        if takes_values and a.startswith("--") and "=" in a:
            name, value = a.split("=", 1)
            if name in PATTERN_OPTIONS:
                patterns.append(value)
            elif name in PATTERN_FILE_OPTIONS:
                pattern_files.append(value)
            i += 1
            continue
        if takes_values and a in PATTERN_OPTIONS | PATTERN_FILE_OPTIONS | other_values:
            if i + 1 < len(args):
                value = args[i + 1]
                if a in PATTERN_OPTIONS:
                    patterns.append(value)
                elif a in PATTERN_FILE_OPTIONS:
                    pattern_files.append(value)
            i += 2
            continue
        if not a.startswith("-"):
            positionals.append(a)
        i += 1
    return positionals, patterns, pattern_files


def _pattern_given_by_flag(args: list[str]) -> bool:
    return any(
        a in ("-e", "-f", "--regexp", "--file") or a.startswith(("--regexp=", "--file="))
        for a in args
    )


def _mentions(text: str, roots: list[str]) -> bool:
    if not text:
        return False
    home = os.path.expanduser("~")
    forms = set(roots)
    for r in roots:
        if r.startswith(home + os.sep):
            rel = r[len(home) + 1 :]
            forms |= {f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"}
    forms |= {"$GENESIS_HOME/holdout", "${GENESIS_HOME}/holdout"}
    return any(_names_component(text, f) for f in forms)


def _names_component(text: str, form: str) -> bool:
    """``form`` appears in ``text`` as a whole path component (not ``holdout-old``)."""
    start = 0
    while True:
        i = text.find(form, start)
        if i < 0:
            return False
        nxt = text[i + len(form) : i + len(form) + 1]
        if not nxt or not (nxt.isalnum() or nxt in "_-."):
            return True
        start = i + 1


# ---------------------------------------------------------------- post


def _post(payload: dict, holdout: str) -> str | None:
    if payload.get("agent_type") in ALLOWED_AGENT_TYPES:
        return None
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = str(payload.get("cwd") or os.getcwd())
    if _post_exempt(str(payload.get("tool_name") or ""), ti, cwd):
        return None
    text = _collapse(_result_text(payload.get("tool_response")))
    if not text:
        return None
    hits = sorted({sid for line, sid in _fingerprints(holdout) if line in text})
    # Only the absolute path: the ~ and $GENESIS_HOME spellings appear in the
    # holdout's own docs, skills and this file, which agents read routinely.
    if not hits and any(_names_component(text, r) for r in _roots(holdout)):
        hits = ["directory path"]
    if not hits:
        return None
    _log(payload, "post", "content", ", ".join(hits))
    return POST_NOTE.format(ids=", ".join(hits))


def _post_exempt(tool: str, ti: dict, cwd: str) -> bool:
    """The runner's own output, or a read of the fence's logs, and nothing else.

    The runner prints only ids and verdicts; the logs name paths by design. Both
    are exempt only when that is ALL the call does: one shell segment running
    the runner, or a read whose every operand is one of the two logs.
    """
    logs = {os.path.normpath(exposure_log()), os.path.normpath(activity_log())}
    if tool == "Read":
        fp = ti.get("file_path")
        return isinstance(fp, str) and _abs(fp, cwd) in logs
    command = ti.get("command") if isinstance(ti.get("command"), str) else ""
    if tool != "Bash" or not command:
        return False
    try:
        from shell_parse import analyze_checked

        segments, blind = analyze_checked(command)
    except Exception:  # noqa: BLE001
        return False
    if blind or len(segments) != 1:
        return False
    argv = list(segments[0].argv or [])
    runs_runner = len(argv) >= 2 and os.path.basename(argv[0]) in INTERPRETERS
    if runs_runner and (
        argv[1].endswith("scripts/holdout_runner.py") or argv[1] == "holdout_runner.py"
    ):
        return True
    operands = [a for a in argv[1:] if not a.startswith("-")]
    return bool(operands) and all(_abs(a, cwd) in logs for a in operands)


def _result_text(resp: object) -> str:
    parts: list[str] = []
    size = 0
    stack = [resp]
    while stack and size < RESULT_SCAN_BYTES:
        node = stack.pop()
        if isinstance(node, str):
            decoded = _json_container(node)
            if decoded is not None:
                stack.append(decoded)  # an MCP result is a JSON string; walk its text
                continue
            parts.append(node)
            size += len(node)
        elif isinstance(node, dict):
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return "\n".join(parts)[:RESULT_SCAN_BYTES]


def _json_container(text: str):
    """The decoded object or list a string holds, else None (bounded)."""
    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[" or len(text) > RESULT_SCAN_BYTES:
        return None
    try:
        import json

        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _fingerprints(holdout: str) -> list[tuple[str, str]]:
    """(collapsed line, scenario id) for every long line of an Expect block."""
    path = os.path.join(holdout, "HOLDOUT.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    sid = "?"
    in_block = slot = False
    fence = ""
    for line in lines:
        stripped = line.strip()
        if in_block:
            if stripped == fence:
                in_block = False
            elif slot:
                collapsed = _collapse(line)
                if len(collapsed) >= MIN_FINGERPRINT_CHARS:
                    out.append((collapsed, sid))
            continue
        m = _HEADING.match(line)
        if m:
            sid, slot = m.group(1), False
            continue
        f = _FENCE.match(stripped)
        if f:
            in_block, fence = True, f.group(1)
            continue
        if stripped:
            slot = stripped == "Expect:"
    return out


# ---------------------------------------------------------------- shared


def _log(payload: dict, event: str, kind: str, detail: object) -> None:
    """Append one row. A failure to log never costs the agent its note."""
    with contextlib.suppress(Exception):
        _append(payload, event, kind, detail)


def _append(payload: dict, event: str, kind: str, detail: object) -> None:
    import fcntl
    import json
    import time

    path = activity_log() if kind in ACTIVITY_KINDS else exposure_log()
    rotate = path == activity_log()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    flat = " ".join(str(detail).split())
    if len(flat) > DETAIL_CHARS:
        flat = flat[:DETAIL_CHARS] + f" [{len(flat) - DETAIL_CHARS} more chars omitted]"
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "kind": kind,
        "session_id": str(payload.get("session_id") or ""),
        "agent_id": str(payload.get("agent_id") or ""),
        "agent_type": str(payload.get("agent_type") or ""),
        "tool": str(payload.get("tool_name") or ""),
        "detail": flat,
    }
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if rotate and os.path.exists(path) and os.path.getsize(path) > LOG_ROTATE_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _emit(event_name: str, note: str) -> None:
    out = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": note}}
    try:
        from hook_output import print_json_bounded

        print_json_bounded(out, text_keys=("hookSpecificOutput.additionalContext",))
    except Exception:  # noqa: BLE001 - the note is short; plain print is safe
        import json

        print(json.dumps(out))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
