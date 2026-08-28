#!/usr/bin/env python3
"""Detect edits that change one member of a set and leave its siblings behind.

The recurring failure this exists for: a fix is applied to the site a reviewer
named, while structurally identical siblings elsewhere keep the old behaviour.
The commit gate already has a mode-switch check for this
(``review_enforcement_commit.py``), but it triggers purely on a counter of
consecutive defect-bearing review rounds — it reads no signal from the diff, so
it cannot speak until two rounds have already been spent. These scans give it a
content signal available on the very first edit.

Stdlib only and no network: this runs inside hooks with single-digit-second
timeouts. Neither entry point raises; both are budget-bounded.
"""

from __future__ import annotations

import ast
import difflib
import subprocess
import time
from pathlib import Path

# Below this length a literal is too generic to be evidence of anything.
MIN_LITERAL_LEN = 16
# Cap the work: only the longest removed literals are worth checking.
MAX_LITERALS_CHECKED = 6
# Hard wall-clock ceiling for one scan. Cost is multiplicative — files x
# literals x repo size — so per-item caps do not bound it: a 20-file changeset
# at the literal cap measured ~6s of `git grep` fan-out across ~2.7k tracked
# files. The deadline is enforced inside EVERY loop, and each subprocess is
# given only the time actually remaining, because a per-call timeout larger
# than the whole budget makes the budget decorative.
SCAN_BUDGET_SECONDS = 1.5
# Never spend the entire remaining budget on one subprocess.
_MAX_SUBPROCESS_SECONDS = 5.0

# Characters that appear VERBATIM in source text, as an ALLOWLIST.
#
# The natural way to write this is a blocklist of escaped characters, and that
# is wrong by construction: every escape the list forgets (\xNN, \uXXXX, octal,
# \a\b\f\v) puts a character into the grep core that never appears in the raw
# file, so the prefilter misses and the scan returns a confident empty result.
# An allowlist can only ever be too CONSERVATIVE — a shorter core, still
# correct — which is the safe direction for a prefilter.
_VERBATIM = frozenset(
    " !#$%&()*+,-./0123456789:;<=>?@"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ[]^_`"
    "abcdefghijklmnopqrstuvwxyz{|}~"
)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


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
        if ch not in _VERBATIM:
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
    that is supposed to change in lockstep.
    """
    stripped = literal.strip()
    return len(stripped) >= MIN_LITERAL_LEN and " " in stripped


def _grep_candidates(core: str, repo_root: Path, deadline: float) -> list[Path]:
    """Files whose raw text contains *core*. Cheap prefilter."""
    budget = min(_remaining(deadline), _MAX_SUBPROCESS_SECONDS)
    if not core or budget <= 0:
        return []
    try:
        out = subprocess.run(
            # -e is required: without it a core beginning with '-' is parsed as
            # an unknown flag, git exits non-zero, and the miss is silent.
            ["git", "grep", "-l", "--fixed-strings", "-z", "-e", core, "--", "*.py"],
            cwd=repo_root, capture_output=True, text=True, timeout=budget,
        ).stdout
    except (subprocess.SubprocessError, OSError, ValueError):
        # ValueError covers an embedded NUL in the pattern, which is neither a
        # SubprocessError nor an OSError and would otherwise propagate out of a
        # function documented as never raising.
        #
        # Unreachable via the only current caller, which passes a core already
        # filtered through the _VERBATIM allowlist (NUL is not in it) — so no
        # test pins this branch, and removing it leaves the suite green. Kept
        # because this is a general helper and the allowlist is the only thing
        # standing between it and a raise; stated rather than left to look like
        # covered ground.
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
    orphans. Never raises, and never runs past *budget_seconds*.
    """
    removed = string_literals(old_source) - string_literals(new_source)
    candidates = sorted(
        (lit for lit in removed if _is_prose(lit)), key=len, reverse=True
    )[:MAX_LITERALS_CHECKED]

    findings: list[dict] = []
    deadline = time.monotonic() + budget_seconds
    for lit in candidates:
        if _remaining(deadline) <= 0:
            break
        survivors = []
        for path in _grep_candidates(searchable_core(lit), repo_root, deadline):
            # Checked per candidate, not just per literal: one common core can
            # match hundreds of files, and parsing them is what actually
            # overruns.
            if _remaining(deadline) <= 0:
                break
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


def _functions_by_qualname(tree: ast.AST) -> dict[str, ast.AST]:
    """Functions keyed by QUALIFIED name (``Class.method``, ``outer.inner``).

    Keying by bare name collides: two classes in one module routinely define
    the same method name — the very file that motivated this scanner has
    ``_capability_performance_section`` in two builders — and a last-one-wins
    map silently compares one class's function against the other's.
    """
    out: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                out[qual] = child
                walk(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _callee_identity(func: ast.AST) -> str:
    """The NAME of the function being called, ignoring its receiver.

    Compare identity, not expression text. Measured over 151 real edits,
    comparing the unparsed expression fired on rewrites that changed only the
    receiver — ``payload.get(k, '').strip`` becoming ``(payload.get(k) or
    '').strip`` calls the same ``strip``, so no use of the result needs
    reconsidering.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _call_sources(fn: ast.AST) -> dict[str, set[str]]:
    """``{variable: {callee, ...}}`` for ``name = some.call(...)`` in *fn*.

    A SET, not a single value: last-assignment-wins hides the motivating case
    outright. ``entries = get_all(db)`` followed by ``entries = normalize(
    entries)`` would record only ``normalize`` in both versions, so swapping
    ``get_all`` for ``get_prompt_rows`` compares equal and vanishes.
    """
    sources: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):  # incl. walrus
            target = node.target
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call):
            callee = _callee_identity(value.func)
            if callee:
                sources.setdefault(target.id, set()).add(callee)
    return sources


def _changed_lines(old_source: str, new_source: str) -> set[int]:
    """1-based line numbers in *new_source* that this edit added or altered."""
    old_lines = old_source.splitlines()
    new_lines = new_source.splitlines()
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            changed.update(range(j1 + 1, j2 + 1))
    return changed


def find_unrevisited_uses(old_source: str, new_source: str) -> list[dict]:
    """Variables whose source changed, with uses the edit left untouched.

    Returns ``[{function, variable, was, now, unrevisited: [line, ...]}]``.
    Never raises.

    Deliberately carries no deadline, unlike the orphan scan. That one needed
    one because its cost scales with REPO size — one common core can match
    hundreds of files, each then parsed. This is two parses and a difflib pass
    over a single file, so it is bounded by input size: measured at 0.28s worst
    case on the two largest files in this repo (171 KiB) with 400 scattered
    edits. A budget here would be untestable ceremony.
    """
    try:
        old_tree, new_tree = ast.parse(old_source), ast.parse(new_source)
    except SyntaxError:
        return []

    old_fns = _functions_by_qualname(old_tree)
    changed = _changed_lines(old_source, new_source)
    findings = []

    for qualname, new_fn in _functions_by_qualname(new_tree).items():
        old_fn = old_fns.get(qualname)
        if old_fn is None:
            continue  # a brand-new function has no prior provenance
        old_sources = _call_sources(old_fn)
        for var, now in _call_sources(new_fn).items():
            was = old_sources.get(var)
            if not was or was == now:
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
                    "function": qualname,
                    "variable": var,
                    "was": "/".join(sorted(was)),
                    "now": "/".join(sorted(now)),
                    "unrevisited": unrevisited,
                })
    return findings
