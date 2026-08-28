#!/usr/bin/env python3
"""Detect edits that change one member of a set and leave its siblings behind.

The recurring failure this exists for: a fix is applied to the site a reviewer
named, while structurally identical siblings elsewhere keep the old behaviour.
The commit gate already has a mode-switch check for this
(``review_enforcement_commit.py``), but it triggers purely on a counter of
consecutive defect-bearing review rounds — it reads no signal from the diff, so
it cannot speak until two rounds have already been spent. These scans give it a
content signal available on the very first edit.

Stdlib only, no imports beyond the standard library, and no network: this runs
inside a PreToolUse/PostToolUse hook with a single-digit-second timeout.

ORPHANED LITERALS
    A string literal is removed or changed in one file while the identical
    value survives elsewhere. Deliberately AST-based: a regex over diff text
    silently skips every literal containing an escape, and prompt strings
    almost all end in ``\\n`` — a regex prototype of this scan reported "no
    findings" precisely because it could not see the class it was written for.
"""

from __future__ import annotations

import ast
import subprocess
import time
from pathlib import Path

# Below this length a literal is too generic to be evidence of anything —
# format fragments, single words, punctuation.
MIN_LITERAL_LEN = 16
# Cap the work: only the longest removed literals are worth checking, and each
# costs a grep.
MAX_LITERALS_CHECKED = 6
# Hard wall-clock ceiling for one scan. Cost is multiplicative — files x literals
# x repo size — so per-item caps alone do not bound it: a 20-file commit at the
# literal cap measured ~6s of `git grep` fan-out across ~2.7k tracked files. An
# advisory that eats a gate's or a tool call's timeout budget is worse than no
# advisory, so the scan abandons what it has not reached rather than overrun.
SCAN_BUDGET_SECONDS = 1.5
# Characters that are escaped in source, so a grep for the literal's raw value
# would not match the file text.
_ESCAPED = set('\\\n\t\r"\'')

_SKIP_DIR_PARTS = frozenset(
    {"__pycache__", ".git", "node_modules", ".venv", "vendor", ".mypy_cache"}
)


def string_literals(source: str) -> set[str]:
    """Every string constant in *source*, by VALUE (escapes already resolved)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def searchable_core(literal: str) -> str:
    """Longest run of *literal* that appears verbatim in source text.

    A literal's value and its source form differ wherever an escape is used, so
    the value cannot be grepped directly. The longest escape-free run can be,
    and is distinctive enough to use as a cheap prefilter before the AST check.
    """
    best = current = ""
    for ch in literal:
        if ch in _ESCAPED:
            current = ""
            continue
        current += ch
        if len(current) > len(best):
            best = current
    return best


def _is_prose(literal: str) -> bool:
    """Is this literal worth checking for siblings?

    Length alone is not enough: measured over 151 real file-edits, the only
    false positive was ``"run_in_background"`` — a 17-character IDENTIFIER that
    legitimately appears in several files. Identifiers and dotted paths get
    duplicated across a repo as a matter of course; prose messages are the thing
    that is supposed to be changed in lockstep. Requiring interior whitespace
    separates the two and took the measured rate to zero.
    """
    stripped = literal.strip()
    return len(stripped) >= MIN_LITERAL_LEN and " " in stripped


def _tracked_python_files(repo_root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "*.py"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    files = []
    for rel in out.split("\0"):
        if not rel:
            continue
        p = repo_root / rel
        if _SKIP_DIR_PARTS.isdisjoint(p.parts):
            files.append(p)
    return files


def _grep_candidates(core: str, repo_root: Path) -> list[Path]:
    """Files whose raw text contains *core*. Cheap prefilter."""
    if not core:
        return []
    try:
        out = subprocess.run(
            # -e is required: without it a core beginning with '-' is parsed as
            # an unknown flag, git exits non-zero, and the miss is silent.
            ["git", "grep", "-l", "--fixed-strings", "-z", "-e", core, "--", "*.py"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [repo_root / rel for rel in out.split("\0") if rel]


def find_orphaned_literals(
    edited: Path,
    old_source: str,
    new_source: str,
    repo_root: Path,
    *,
    budget_seconds: float = SCAN_BUDGET_SECONDS,
) -> list[dict]:
    """Literals this edit removed that still exist in other files.

    Returns ``[{literal, survivors: [path, ...]}]``, empty when the edit left no
    orphans. Never raises: a hook must not break the tool call it observes.
    """
    removed = string_literals(old_source) - string_literals(new_source)
    candidates = sorted(
        (lit for lit in removed if _is_prose(lit)),
        key=len,
        reverse=True,
    )[:MAX_LITERALS_CHECKED]

    findings = []
    deadline = time.monotonic() + budget_seconds
    for lit in candidates:
        if time.monotonic() >= deadline:
            break  # out of budget: report what was found, do not overrun
        survivors = []
        for path in _grep_candidates(searchable_core(lit), repo_root):
            if path.resolve() == edited.resolve():
                continue
            try:
                # AST-confirm: the grep matched raw text, which can differ from
                # the literal's value wherever an escape is involved.
                if lit in string_literals(path.read_text(encoding="utf-8")):
                    survivors.append(path)
            except (OSError, UnicodeDecodeError):
                continue
        if survivors:
            findings.append({"literal": lit, "survivors": survivors})
    return findings


# --------------------------------------------------------------------------
# Changed provenance
# --------------------------------------------------------------------------
#
# When an edit changes what a variable is assigned FROM, every later use of
# that variable is now reading something different and each one has to be
# reconsidered. Missing one is silent: the code still runs, and the use just
# quietly means something else than it did.
#
# The motivating case: `entries = get_all(...)` became
# `entries = get_prompt_rows(...)`, which changed `entries` from "the whole
# table" to "the qualifying subset". Two of its four uses were revisited. One of
# the two that were not computed an average and rendered it as a whole-map
# figure.


def _functions_by_name(tree: ast.AST) -> dict[str, ast.AST]:
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _call_sources(fn: ast.AST) -> dict[str, str]:
    """``{variable: callee}`` for simple ``name = some.call(...)`` assignments."""
    sources = {}
    for node in ast.walk(fn):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call):
            callee = _callee_identity(value.func)
            if callee:
                sources[target.id] = callee
    return sources


def _callee_identity(func: ast.AST) -> str:
    """The NAME of the function being called, ignoring its receiver.

    Compare identity, not expression text. Measured over 151 real edits,
    comparing the unparsed expression fired on rewrites that changed only the
    receiver — ``payload.get(k, '').strip`` becoming
    ``(payload.get(k) or '').strip`` calls the same ``strip``, so no use of the
    result needs reconsidering. A change of the called function itself
    (``get_all`` -> ``get_prompt_rows``) is the signal worth raising.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _changed_lines(old_source: str, new_source: str) -> set[int]:
    """1-based line numbers in *new_source* that this edit added or altered."""
    import difflib

    old_lines = old_source.splitlines()
    new_lines = new_source.splitlines()
    changed = set()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            changed.update(range(j1 + 1, j2 + 1))
    return changed


def find_unrevisited_uses(old_source: str, new_source: str) -> list[dict]:
    """Variables whose source changed, with uses the edit left untouched.

    Returns ``[{function, variable, was, now, unrevisited: [line, ...]}]``.
    Never raises.
    """
    try:
        old_tree, new_tree = ast.parse(old_source), ast.parse(new_source)
    except SyntaxError:
        return []

    old_fns = _functions_by_name(old_tree)
    changed = _changed_lines(old_source, new_source)
    findings = []

    for name, new_fn in _functions_by_name(new_tree).items():
        old_fn = old_fns.get(name)
        if old_fn is None:
            continue  # a brand-new function has no prior provenance
        old_sources = _call_sources(old_fn)
        for var, now in _call_sources(new_fn).items():
            was = old_sources.get(var)
            if was is None or was == now:
                continue
            uses = sorted(
                node.lineno
                for node in ast.walk(new_fn)
                if isinstance(node, ast.Name)
                and node.id == var
                and isinstance(node.ctx, ast.Load)
            )
            unrevisited = [ln for ln in uses if ln not in changed]
            if unrevisited:
                findings.append({
                    "function": name,
                    "variable": var,
                    "was": was,
                    "now": now,
                    "unrevisited": unrevisited,
                })
    return findings
