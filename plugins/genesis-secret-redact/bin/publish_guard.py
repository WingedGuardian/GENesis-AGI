#!/usr/bin/env python3
"""PreToolUse hook: refuse a `gh` command that would publish a known secrets.env value.

The plugin keeps values out of what a session reads, so a session rarely has one to type. This is the backstop
for the costliest leak there is, a value reaching a public pull request, issue or comment: a Bash or Monitor
command that mentions `gh` is denied when a known value appears in the command text, in a word the shell would
pass to a command, or in an existing file the command names (a `--body-file`, a `-F field=@file`, a `<file`
redirect, a `$(cat file)`, or any other spelling: every word, redirect target and assignment value is tried as a
path). Nothing about gh's flags is modelled; the spellings that are not followed are listed in
docs/reference/secret-redaction.md ("Not covered").

All shell grammar comes from the repository's shared parser, `scripts/hooks/shell_parse.py`: words, nested
commands (`bash -c`, `$(...)`, backticks), redirect targets, assignments and the directories `cd`, `pushd` and
`env -C` move into. A command the parser cannot prove it read completely cannot be checked. It is refused, with
the parser's own reason and the way out, when any gh in it may publish (`pr create|comment|edit|review|merge`,
`issue create|comment|edit`, `api`, `release`, `gist`, or a gh whose subcommand the text does not clearly show);
when every gh in it clearly only reads (`pr view`, `run list`, `auth status`...), and no value is in its text, it
goes ahead with a note to the agent that the value check did not run.

`gh secret` and `gh auth` are left alone: sending a value to GitHub's secret store or logging in with a token is
what they are for. The verdict names the variable and where it was found, never the value.

Pushed commits are checked by the repository's own pre-push scan, not here.

Fail direction: if the values cannot be loaded, the call goes ahead and one line on stderr says the check did not
run (a backstop must not stop work it cannot judge). If the parser cannot be loaded, only the raw command text is
checked. A value already found in the command text is never let through because a later step failed.
"""

from __future__ import annotations

import glob
import importlib.util
import itertools
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

_BIN = Path(__file__).resolve().parent
_EXEMPT = {"secret", "auth"}
#: `gh` as a word of its own: not `ghost`, not `gh-pages`, not `.gh`.
_GH_WORD = re.compile(r"(?<![\w.-])gh(?![\w.-])")
#: Files are read in pieces of this many characters, up to the cap; a longer file is not checked past the cap.
_CHUNK = 1024 * 1024
_MAX_FILE = 16 * 1024 * 1024
#: How many files one glob word may name.
_MAX_GLOB = 64
#: How long the file scan may run. The hook's timeout is 15 s, and a hook killed by it decides nothing.
_SCAN_SECONDS = 8.0
#: `cd`/`pushd` options that come before the directory.
_CD_OPTIONS = {"--", "-L", "-P", "-e", "-@"}


def _load_values() -> tuple[Path, list[dict]]:
    """The checkout root the resolver chose, and the needles ``{var, text, placeholder}``."""
    spec = importlib.util.spec_from_file_location("_publish_known_values", _BIN / "known_values.py")
    if spec is None or spec.loader is None:
        raise ImportError("known_values.py")
    known = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(known)
    root = known.repo_root(str(_BIN.parents[2]))
    return root, known.load(root)["needles"]


def _load_parser(root: Path):
    """The shared shell parser, by path. It must be in sys.modules before it runs: its dataclasses look
    their own module up there, and under ``python3 -I`` the lookup otherwise fails."""
    path = root / "scripts" / "hooks" / "shell_parse.py"
    spec = importlib.util.spec_from_file_location("_publish_shell_parse", path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _exempt(parser, segs) -> bool:
    """Whether every gh this command runs is PROVEN to be `gh secret` or `gh auth`."""
    ghs = [s for s in segs if s.exe == "gh"]
    if not ghs:
        return False
    for s in segs:
        if s.verb_unresolved or (s.argv and ("$" in s.argv[0] or "`" in s.argv[0])):
            return False
        if s.exe != "gh" and any(_GH_WORD.search(w) for w in s.argv):
            return False  # gh run by another program (`find -exec gh`, `ssh host gh`): not proven exempt
    for s in ghs:
        invocation = parser.gh_command(s.argv)
        if invocation is None or invocation.group not in _EXEMPT:
            return False
    return True


def _words(segs) -> list[str]:
    """Every word the command hands a program, at every depth: argv words, assignment values and the words of
    file redirect targets (the parser drops a plain target from argv, so a `<file` is only here)."""
    words: list[str] = []
    for s in segs:
        words.extend(s.argv)
        for kind, value in s.contexts:
            if kind == "assignment" and value and "=" in value:
                words.append(value.split("=", 1)[1])
        for op, target in s.targets:
            if not target or op in ("<<", "<<<"):
                continue  # a here-doc delimiter or here-string text is already in the command text
            if op.endswith("&") and (target.isdigit() or target == "-"):
                continue  # an fd duplication, not a file
            if "$(" in target or "`" in target:
                continue  # its command is already a nested segment; `$HOME` is left to _expand
            try:
                words.extend(shlex.split(target))
            except ValueError:
                continue
    return words


def _expand(word: str, home: str) -> str | None:
    """``word`` with a leading `~`, `$HOME` or `${HOME}` resolved; None when any other expansion is left."""
    for prefix in ("${HOME}", "$HOME"):
        if word == prefix or word.startswith(prefix + "/"):
            word = home + word[len(prefix) :]
            break
    else:
        if word == "~" or word.startswith("~/"):
            word = home + word[1:]
    if "$" in word or "`" in word:
        return None
    return word


def _bases(segs, cwd: str, home: str) -> list[str]:
    """Every directory a relative path in the command can be read from: the payload's cwd and each directory
    the command moves into, cumulatively. A union, so a subshell's `cd` only adds directories to check."""
    bases, cur = [cwd], cwd
    for s in segs:
        for kind, value in s.contexts:
            if kind == "chdir" and value:
                target = _expand(value, home)
                if target is not None:
                    bases.append(os.path.normpath(os.path.join(cur, target)))
        if s.exe in ("cd", "pushd"):
            args = [a for a in s.argv[1:] if a not in _CD_OPTIONS]
            if not args:
                target = home if s.exe == "cd" else None
            elif args[0] == "-" or re.fullmatch(r"[+-]\d+", args[0]):
                target = None
            else:
                target = _expand(args[0], home)
            if target is not None:
                cur = os.path.normpath(os.path.join(cur, target))
                bases.append(cur)
    return list(dict.fromkeys(bases))


def _spellings(word: str) -> set[str]:
    """The ways a word can name a file: itself, a short option's attached value (`-Fbody.md`), every part after
    each `=` (`--field=body=@f` gives `body=@f` and `@f`), and each of those without a leading `@`."""
    out = {word}
    if word.startswith("-") and not word.startswith("--") and len(word) > 2:
        out.add(word[2:])
    for w in list(out):
        parts = w.split("=")
        out.update("=".join(parts[i:]) for i in range(1, len(parts)))
    for w in list(out):
        out.add(w.lstrip("@"))
    return {w for w in out if w and w != "-" and not w.startswith("-")}


def _scan_file(path: str, needles: list[dict]) -> tuple[set[str], bool]:
    """The variables whose value is in the file, and whether the file runs past the cap unread."""
    hits: set[str] = set()
    longest = max(len(n["text"]) for n in needles)
    tail, read = "", 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                return hits, False
            text = tail + chunk
            hits |= {n["var"] for n in needles if n["text"] in text}
            tail = text[-(longest - 1) :] if longest > 1 else ""
            read += len(chunk)
            if read >= _MAX_FILE:
                return hits, bool(fh.read(1))


def _file_findings(
    segs, words: list[str], cwd: str, needles: list[dict]
) -> tuple[list[str], list[str]]:
    """'VAR in <path>' for each named file holding a value, and notes for the agent on what was not checked
    (a file over the cap, a glob cut at its limit, a scan stopped at its deadline)."""
    home = os.environ.get("HOME") or str(Path.home())
    deadline = time.monotonic() + _SCAN_SECONDS
    found: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    candidates = [_expand(c, home) for w in dict.fromkeys(words) for c in _spellings(w)]
    candidates = list(dict.fromkeys(c for c in candidates if c))
    bases = _bases(segs, cwd, home)
    for cand in candidates:
        for joined in [cand] if os.path.isabs(cand) else [os.path.join(b, cand) for b in bases]:
            if time.monotonic() > deadline:
                notes.append(
                    f"secret-redact: the file check stopped after {_SCAN_SECONDS:g} s, so some files this "
                    "command names were not checked for secrets.env values."
                )
                return found, notes
            try:
                if any(c in cand for c in "*?["):
                    paths = list(itertools.islice(glob.iglob(joined), _MAX_GLOB + 1))
                    if len(paths) > _MAX_GLOB:
                        paths = paths[:_MAX_GLOB]
                        notes.append(
                            f"secret-redact: {joined} names more than {_MAX_GLOB} files; only the first "
                            f"{_MAX_GLOB} were checked for secrets.env values."
                        )
                else:
                    paths = [joined]
                for path in paths:
                    path = os.path.normpath(path)
                    if path in seen or not os.path.isfile(path):
                        continue
                    seen.add(path)
                    hits, over = _scan_file(path, needles)
                    found.extend(f"{var} in {path}" for var in sorted(hits))
                    if over and not hits:
                        notes.append(
                            f"secret-redact: {path} is over 16 MiB and was not checked for secrets.env values."
                        )
            except (OSError, ValueError, RuntimeError):
                continue
    return found, notes


def _redact(text: str, needles: list[dict]) -> str:
    for n in needles:  # longest text first, as the loader orders them
        text = text.replace(n["text"], n.get("placeholder") or f"[secrets.env:{n['var']}]")
    return text


def _emit(needles: list[dict], reason: str | None, notes: list[str]) -> None:
    out: dict = {"hookEventName": "PreToolUse"}
    if reason is not None:
        out["permissionDecision"] = "deny"
        out["permissionDecisionReason"] = _redact(reason, needles)
    if notes:
        out["additionalContext"] = _redact(" ".join(notes), needles)
    if len(out) > 1:
        json.dump({"hookSpecificOutput": out}, sys.stdout)


def _value_reason(found: list[str]) -> str:
    return (
        "Refused: this gh command carries, or names a file holding, a secrets.env value ("
        + "; ".join(found)
        + "). Take the value out of the text or file; refer to the key by its variable name instead."
    )


#: gh commands that only read, by group, and the read-only verbs of the groups that also publish. Anything
#: not listed here, including a verb the text cannot show (`gh "$V"`), counts as publishing.
_READ_GROUPS = {
    "auth",
    "secret",
    "search",
    "browse",
    "status",
    "help",
    "version",
}
_READ_VERBS = {
    "pr": {"view", "list", "checks", "status", "diff", "checkout"},
    "issue": {"view", "list", "status"},
    "repo": {"view", "list", "clone"},
    "run": {"view", "list", "watch", "download"},
    "workflow": {"view", "list"},
}
_GH_GLOBAL_WITH_VALUE = {"-R", "--repo"}
_WORD = re.compile(r"[A-Za-z][\w-]*")


def _gh_reads_only(text: str, start: int) -> bool:
    """Whether the gh mention ending at ``start`` is clearly a read-only command."""
    words = text[start:].split(None, 8)
    while words and (words[0] in _GH_GLOBAL_WITH_VALUE or words[0].startswith(("--repo=", "-R"))):
        words = words[2:] if words[0] in _GH_GLOBAL_WITH_VALUE else words[1:]
    if not words:
        return True  # a bare `gh` prints its help
    group = _WORD.fullmatch(words[0].rstrip(";&|)"))
    if group is None:
        return False
    group_name = group.group(0)
    if group_name in _READ_GROUPS:
        return True
    verb = _WORD.fullmatch(words[1].rstrip(";&|)")) if len(words) > 1 else None
    return verb is not None and verb.group(0) in _READ_VERBS.get(group_name, set())


def _may_publish(parser, command: str) -> bool:
    """For a command the parser could not read: whether any gh mention, in the raw text or with quotes,
    backslashes and line continuations removed, might be a publishing command. Deliberately lenient: only a
    gh that is clearly a read is cleared."""
    for view in parser.mention_views(command):
        for match in _GH_WORD.finditer(view):
            if not _gh_reads_only(view, match.end()):
                return True
    return False


#: Programs that run a command handed to them as one string, beside the parser's own `_REPARSE_CARRIERS`.
_LAUNCHERS = {"eval", "tmux", "screen", "parallel", "script", "watch", "flock", "su", "systemd-run"}


def _open_carriers(parser, segs):
    """Segments of the commands other programs are handed as one string (`script -qc "gh ..."`, `watch`,
    `flock -c`, `su -c`, `tmux new-session`, `parallel`, `eval`...), which the parser names but does not read.
    Each word of a non-gh segment that mentions gh and holds whitespace is parsed as a command, to the parser's
    own depth bound. Returns the segments found and a blind spot when a LAUNCHER's string cannot be read. Any
    other program's unreadable string (a `git commit -m` or `echo` message that mentions gh and holds an
    apostrophe) is data, not a command, and is skipped: treating those as unread refused 71 of 14,019 real
    gh-mentioning commands that nothing else refused (measured 2026-11-13), and none ran gh."""
    launchers = set(getattr(parser, "_REPARSE_CARRIERS", ())) | _LAUNCHERS
    found, todo = [], [(s, 0) for s in segs]
    while todo:
        seg, depth = todo.pop()
        if seg.exe == "gh" or depth >= parser.MAX_SUBSTITUTION_DEPTH:
            continue
        for word in seg.argv[1:]:
            if not (_GH_WORD.search(word) and any(c.isspace() for c in word)):
                continue
            inner, blind = parser.analyze_policy(word)
            if blind is not None:
                if seg.exe in launchers:
                    return found, blind
                continue
            found.extend(inner)
            todo.extend((s, depth + 1) for s in inner)
    return found, None


def _raw_mentions_gh(command: str) -> bool:
    """The gate when the parser is unavailable: `gh` in the text, or once quotes and backslashes are gone."""
    return "gh" in command or "gh" in re.sub(r"[\"'\\\\]", "", command)


def _decide(command: str, cwd: str, root: Path, needles: list[dict], text_found: list[str]) -> None:
    try:
        parser = _load_parser(root)
    except Exception as exc:  # noqa: BLE001 - a broken checkout degrades to the raw-text check
        if not _raw_mentions_gh(command):
            return
        if text_found:
            _emit(needles, _value_reason(text_found), [])
        else:
            print(
                f"secret-redact: shell parser did not load ({type(exc).__name__}); raw text checked only",
                file=sys.stderr,
            )
        return
    if not parser.mentions(command, _GH_WORD):
        return
    segs, blind = parser.analyze_policy(command)
    if blind is None:
        carried, blind = _open_carriers(parser, segs)
        segs = segs + carried
    if blind is not None:  # an unread command cannot be proven to be `gh secret` either
        if not text_found and not _may_publish(parser, command):
            note = (
                f"secret-redact: the shell parser cannot read all of this command (it {blind.cause}), so it "
                "was not checked for secrets.env values; it was let through because no gh in it creates or edits "
                "a pull request, issue, release or gist or calls the API."
            )
            print(_redact(note, needles), file=sys.stderr)
            _emit(needles, None, [note])
            return
        reason = (
            f"Refused: this command runs gh, and the shell parser cannot read all of it: it {blind.cause}. "
            "A gh command is checked for secrets.env values before it runs, and a command that cannot be read "
            f"cannot be checked. To proceed: {blind.hint}. A body written to a file and passed with --body-file "
            "keeps the command readable."
        )
        if text_found:
            reason += f" It also carries a secrets.env value ({'; '.join(text_found)})."
        _emit(needles, reason, [])
        return
    if _exempt(parser, segs):
        return
    found = list(text_found)
    words = _words(segs)
    for n in needles:
        place = f"{n['var']} in the command"
        if place not in found and any(n["text"] in w for w in words):
            found.append(place)
    if found:  # a value in the text is enough; the file scan could only delay the refusal
        _emit(needles, _value_reason(found), [])
        return
    found, notes = _file_findings(segs, words, cwd, needles)
    for note in notes:
        print(_redact(note, needles), file=sys.stderr)
    _emit(needles, _value_reason(found) if found else None, notes)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") not in ("Bash", "Monitor"):
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            return 0
        cwd = payload.get("cwd") or os.getcwd()
        root, needles = _load_values()
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        print(f"secret-redact: publish check did not run ({type(exc).__name__})", file=sys.stderr)
        return 0
    if not needles:
        return 0
    # Found before any parse, and kept whatever a later step does.
    text_found = [f"{n['var']} in the command" for n in needles if n["text"] in command]
    text_found = list(dict.fromkeys(text_found))
    try:
        _decide(command, cwd, root, needles, text_found)
    except Exception as exc:  # noqa: BLE001 - a later failure never discards a value already found
        if text_found and _raw_mentions_gh(command):
            _emit(needles, _value_reason(text_found), [])
        else:
            print(
                f"secret-redact: publish check did not finish ({type(exc).__name__})",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
