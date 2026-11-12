#!/usr/bin/env python3
"""PreToolUse hook: refuse a `gh` command that would publish a known secrets.env value.

The plugin keeps values out of what a session reads, so a session rarely has one to type. This is the backstop
for the costliest leak there is, a value reaching a public pull request, issue or comment: a Bash command with a
`gh` segment is denied when a known value appears in the command text, or in an existing file the command names
(a `--body-file`, a `-F field=@file`, or any other spelling: every token, and the part after `=` in a token, is
checked as a possible path). Nothing about gh's flags is modelled, so no spelling of a body escapes the check.

`gh secret` and `gh auth` are left alone: sending a value to GitHub's secret store or logging in with a token is
what they are for. The verdict names the variable and where it was found, never the value.

Pushed commits are checked by the repository's own pre-push scan, not here.

Fail direction: if the values cannot be loaded or the command cannot be examined, the call goes ahead and one line
on stderr says the check did not run (a backstop must not stop work it cannot judge).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent
_MAX_FILE = 1024 * 1024
_EXEMPT = {"secret", "auth"}
#: Where a segment starts: the command's start, or after a separator or an opening bracket/backtick.
#: `gh <subcommand>` anywhere a word can start, so wrappers (`timeout 60 gh`, `env gh`, `then gh`, `bash -c "gh`)
#: are caught too. Narrowing this to segment starts let 11 of 12 wrapper spellings through (measured); a broad
#: gate costs almost nothing because the second condition, a known value present, is already narrow.
_SEGMENT = re.compile(r"(?<![\w.-])(?:\S*/)?gh\s+([^\s'\"]+)")
_CD = re.compile(r"(?<![\w.-])cd\s+([^\s;&|)]+)")


def _gh_publishes(command: str) -> bool:
    """Whether any segment runs `gh <subcommand>` other than the exempt ones."""
    return any(sub not in _EXEMPT for sub in _SEGMENT.findall(command))


def _needles(root: Path) -> list[dict]:
    spec = importlib.util.spec_from_file_location("_publish_known_values", _BIN / "known_values.py")
    if spec is None or spec.loader is None:
        raise ImportError("known_values.py")
    known = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(known)
    return known.load(known.repo_root(str(root)))["needles"]


def _named_files(command: str, cwd: str) -> list[Path]:
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        tokens = command.split()
    # A relative path may be meant from the payload's cwd or from a directory the command `cd`s into first.
    bases = [Path(cwd)]
    for target in _CD.findall(command):
        target_path = Path(target.strip("'\"")).expanduser()
        bases.append(target_path if target_path.is_absolute() else Path(cwd) / target_path)
    paths: list[Path] = []
    for token in tokens:
        for candidate in {
            token,
            token.split("=", 1)[-1],
            token.split("=", 1)[-1].lstrip("@"),
            token.lstrip("@"),
        }:
            if not candidate or candidate.startswith("-"):
                continue
            raw = Path(candidate).expanduser()
            for path in [raw] if raw.is_absolute() else [b / raw for b in bases]:
                try:
                    if path.is_file() and path.stat().st_size <= _MAX_FILE and path not in paths:
                        paths.append(path)
                except OSError:
                    continue
    return paths


def findings(command: str, cwd: str, needles: list[dict]) -> list[str]:
    """Where known values appear, as 'VAR in <place>' (no value in the text)."""
    if not needles or not _gh_publishes(command):
        return []
    found: list[str] = []
    for n in needles:
        if n["text"] in command and f"{n['var']} in the command" not in found:
            found.append(f"{n['var']} in the command")
    for path in _named_files(command, cwd):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n in needles:
            if n["text"] in text and f"{n['var']} in {path}" not in found:
                found.append(f"{n['var']} in {path}")
    return found


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") not in ("Bash", "Monitor"):
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str) or "gh" not in command:
            return 0
        found = findings(command, payload.get("cwd") or os.getcwd(), _needles(_BIN.parents[2]))
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        print(f"secret-redact: publish check did not run ({type(exc).__name__})", file=sys.stderr)
        return 0
    if found:
        reason = (
            "Refused: this gh command carries, or names a file holding, a secrets.env value ("
            + "; ".join(found)
            + "). "
            "Take the value out of the text or file; refer to the key by its variable name instead."
        )
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
