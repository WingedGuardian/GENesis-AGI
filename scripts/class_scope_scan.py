#!/usr/bin/env python3
"""Detect edits that change one member of a set and leave its siblings behind.

The recurring failure this exists for: a fix is applied to the site a reviewer
named, while structurally identical siblings elsewhere keep the old behaviour.
The commit gate already has a mode-switch check for this
(``review_enforcement_commit.py``), but it triggers purely on a counter of
consecutive defect-bearing review rounds — it reads no signal from the diff, so
it cannot speak until two rounds have already been spent. These scans give it a
content signal available on the very first edit.

Stdlib only and no network. Run it deliberately -- see the CLI at the bottom
of this file, and the entry in the genesis-development skill's review protocol.
"""

from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import difflib
import subprocess
import sys
import time
from pathlib import Path

# Below this length a literal is too generic to be evidence of anything.
MIN_LITERAL_LEN = 16
# Cap the work: only the longest removed literals are checked.
#
# MEASURED 2026-08-28 against 95 real class-divergences mined from 1,584 commits
# of this repo's history (a literal removed from one file, then removed again
# from a second file later — history recording that the first fix left a
# sibling). Recall on the 47 in-scope cases, against fire rate on 100 benign
# commits and worst-case runtime on the largest real changeset:
#
#     cap    recall     fire rate    worst case
#       6     57%          4%          1.5s      <- the old hook-timeout value
#      50     96%          6%          3.7s      <- the knee, shipped
#     400     98%          6%          8.1s
#
# Those recall figures are the CAP's contribution alone, measured before the
# core-extraction and same-file-survivor fixes in this change; with those, cap=50
# reaches 47/47 on the same corpus. Quoting 96% here and 100% elsewhere is not a
# contradiction, but it is only legible if the difference is stated -- so it is.
#
# 6 was never a considered choice; it was whatever fit a 10s PreToolUse budget,
# and it cost 39 points of recall to save 2 points of fire rate. Now that this
# is a script the operator runs deliberately, the budget is not the constraint.
# Override with --max-literals for an exhaustive pass over a large refactor.
MAX_LITERALS_CHECKED = 50
# Hard wall-clock ceiling for one scan. Cost is multiplicative — files x
# literals x repo size — so per-item caps do not bound it: a 20-file changeset
# at the literal cap measured ~6s of `git grep` fan-out across ~2.7k tracked
# files. The deadline is enforced inside EVERY loop, and each subprocess is
# given only the time actually remaining, because a per-call timeout larger
# than the whole budget makes the budget decorative.
SCAN_BUDGET_SECONDS = 30.0
# Never spend the entire remaining budget on one subprocess.
_MAX_SUBPROCESS_SECONDS = 5.0
# Below this, a core is too generic to prefilter usefully; grepping it would
# fan out over most of the tree. Such a literal is REPORTED as unchecked rather
# than silently dropped.
_MIN_CORE_LEN = 12

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
    return set(literal_counts(source))


def literal_counts(source: str) -> collections.Counter[str]:
    """Every string constant with its OCCURRENCE COUNT.

    Counts, not a set: comparing sets makes a change invisible whenever the
    edited file still holds another copy of the old text. The value stays in
    both sets, the difference is empty, and the scanner never looks at the
    siblings — silently, on the exact shape it exists to catch.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return collections.Counter()
    return collections.Counter(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def source_cores(source: str, wanted: set[str] | None = None) -> dict[str, str]:
    """``{literal value: greppable core taken from its SOURCE TEXT}``.

    The core must come from the source, not the value. For an implicitly
    concatenated literal — ``("old prose " "continued here")`` — the AST hands
    back the joined value, which never appears contiguously in the file, so a
    core derived from it is a pattern `git grep` can never match and the sibling
    is skipped in silence.

    *wanted* restricts the work to the literals the caller will actually search.
    That is not a micro-optimisation: ``ast.get_source_segment`` re-splits the
    WHOLE source on every call, so computing cores for every literal in a file
    is O(literals x filesize). MEASURED at 500/1000/2000/4000 literals:
    0.27s / 1.06s / 4.23s / 16.99s — clean 4x per doubling, and 5.8s on one real
    170 KiB file in this repo. Because it used to run over every literal
    regardless of the cap, `--max-literals` could not bound the dominant cost
    and the stated scan budget did not cover it at all.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    cores: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if wanted is not None and node.value not in wanted:
            continue
        segment = ast.get_source_segment(source, node) or ""
        core = _core_present_in(node.value, segment) if segment else ""
        # Keep the longest core seen for a given value — a value written
        # several ways should be searched by its most distinctive rendering.
        if len(core) > len(cores.get(node.value, "")):
            cores[node.value] = core
    return cores


def _core_present_in(value: str, segment: str) -> str:
    """Longest escape-free run of *value* that actually occurs in *segment*.

    Taking the core from the segment text alone is WRONG, and measurably so.
    CPython gives an implicitly concatenated string ONE Constant node whose
    source span covers the entire group, so the segment can contain a different
    fragment, the quotes, an ``f`` prefix, or a trailing comment. Measured on
    this repo (measured at commit c608d158 in `memory/graph_expansion.py`; the
    same construct lives today at `memory/retrieval.py:613`) -- a three-line
    f-string group with an inline
    ``# noqa``), the old code produced the core
    ``'  # noqa: S608 - literal fragments; values bound'`` — so the prefilter
    grepped a COMMENT, matched nothing, and the scan reported a confident clean.

    Taking it from the value alone is also wrong: escapes mean the value's
    characters are not the file's characters.

    So take the value's escape-free run and keep only as much of it as appears
    in the node's own source. Shrinking is safe — this is a prefilter, and a
    shorter core is merely less selective. A core drawn from the wrong text is
    not safe at all. Prefixes are monotone (a shorter prefix of a present
    prefix is present), so the longest is found by bisection.
    """
    vcore = searchable_core(value)
    if not vcore or vcore in segment:
        return vcore
    lo, hi = 0, len(vcore)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if vcore[:mid] in segment:
            lo = mid
        else:
            hi = mid - 1
    return vcore[:lo]


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


class GrepUnavailable(RuntimeError):
    """`git grep` could not answer — as distinct from answering "no matches".

    This is the same distinction `GitError` draws for the other git calls, and
    it belongs here most of all: this is the call whose result BECOMES the
    verdict. Returning [] on a failed grep reports a confident "no survivors"
    for a question that was never asked, which is precisely the defect this
    tool exists to name in other people's code.
    """


def _grep_candidates(core: str, repo_root: Path, deadline: float) -> list[Path]:
    """Files whose raw text contains *core*. Cheap prefilter.

    Raises :class:`GrepUnavailable` when git could not answer — a non-zero exit
    other than 1 (git grep uses 0=match, 1=no match, >=2=error), a timeout, or a
    failure to spawn. The caller records those as UNCHECKED rather than clean.
    """
    budget = min(_remaining(deadline), _MAX_SUBPROCESS_SECONDS)
    if not core:
        return []
    if budget <= 0:
        raise GrepUnavailable("no time left in the scan budget")
    try:
        out = subprocess.run(
            # -e is required: without it a core beginning with '-' is parsed as
            # an unknown flag, git exits non-zero, and the miss is silent.
            # --untracked: a sibling created in this same change is not yet
            # tracked, and plain `git grep` would not see it — the scanner would
            # report a confident clean on exactly the copy-paste case it exists
            # to catch.
            ["git", "grep", "-l", "--fixed-strings", "-z", "--untracked",
             "-e", core, "--", "*.py"],
            cwd=repo_root, capture_output=True, text=True, timeout=budget,
        )
    except subprocess.TimeoutExpired as exc:
        raise GrepUnavailable(f"git grep timed out after {budget:.1f}s") from exc
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        # ValueError covers an embedded NUL in the pattern, which is neither a
        # SubprocessError nor an OSError. Unreachable via the only current
        # caller (its cores pass the _VERBATIM allowlist, which excludes NUL),
        # but this is a general helper and the allowlist is the only thing
        # standing between it and a raise.
        raise GrepUnavailable(f"git grep could not run: {exc}") from exc
    # git grep: 0 = matched, 1 = no match, >= 2 = error. Reading only stdout
    # made every one of those errors — not a repository, a corrupt index, a bad
    # pathspec, a mid-merge tree — indistinguishable from "no matches".
    if out.returncode not in (0, 1):
        detail = (out.stderr or "").strip().splitlines()
        raise GrepUnavailable(
            detail[0] if detail else f"git grep exit {out.returncode}"
        )
    return [repo_root / rel for rel in out.stdout.split("\0") if rel]


def find_orphaned_literals(
    edited: Path,
    old_source: str,
    new_source: str,
    repo_root: Path,
    *,
    budget_seconds: float = SCAN_BUDGET_SECONDS,
    max_literals: int | None = None,
    skipped: list[dict] | None = None,
    examined: list[str] | None = None,
) -> list[dict]:
    """Literals this edit removed that still exist in other files.

    Returns ``[{literal, survivors: [path, ...]}]`` -- plus a
    ``search_incomplete`` key on any finding whose search could not be
    completed, so a partial answer is never read as a whole one. Empty when the
    edit left no
    orphans. Never runs past *budget_seconds*. Raises ``ValueError`` for a
    *max_literals* below 1 -- a caller bug, not an input the scan can report
    on; every other failure is REPORTED (as a finding or a skip), not raised.

    Two optional out-params make the result INTERPRETABLE, which is the whole
    point of this tool: an empty finding list means "clean" only if you also
    know what was looked at.

    - *skipped* receives ``{literal, reason}`` for everything NOT checked.
    - *examined* receives every literal carried to a definite verdict.

    Together they satisfy the invariant the scan is built around: every
    candidate lands in exactly one of {finding, skipped, examined-clean}, and
    never in two. That is asserted directly by the test suite rather than left
    as an intention, because the failure it guards against — a silent exit path
    that reads downstream as "nothing to report" — is the defect this scanner
    exists to find in other code, and it has shipped inside this scanner twice.
    """
    old_counts = literal_counts(old_source)
    new_counts = literal_counts(new_source)
    # A DECREASE in occurrences, not mere absence: changing one of two copies in
    # the same file leaves the value present, and set difference sees nothing.
    removed = {lit for lit, n in old_counts.items() if new_counts[lit] < n}
    prose = sorted((lit for lit in removed if _is_prose(lit)), key=len, reverse=True)
    if max_literals is not None and max_literals < 1:
        # `main` validates at parse time, but this function is exported and
        # directly tested, so the guarantee has to live where the slicing is:
        # a negative limit makes `prose[:limit]` check everything but the last
        # literal and then report that one as over the cap.
        raise ValueError(f"max_literals must be >= 1 or None, got {max_literals}")
    limit = MAX_LITERALS_CHECKED if max_literals is None else max_literals
    candidates = prose[:limit]

    # THE DEADLINE IS TAKEN FIRST, before any work it is supposed to bound.
    # `source_cores` used to run above this line and over EVERY literal in the
    # file, so the dominant cost of the whole scan sat outside the budget it
    # claimed to enforce, and neither --budget nor --max-literals could reduce
    # it. Cores are now computed only for the capped candidate set.
    deadline = time.monotonic() + budget_seconds
    cores = source_cores(old_source, set(candidates))

    # THE INVARIANT this function is built around: every candidate literal ends
    # in EXACTLY ONE of three states — a finding, a `skipped` entry naming why
    # it was not checked, or `examined` (carried to a definite clean verdict).
    # No path may exit silently, because a scanner that examined nothing and one
    # that found nothing produced identical output before, and this one has been
    # both.
    #
    # `_seen` guards the opposite error — a literal recorded twice inflates the
    # "not checked" denominator, which is the number this tool asks to be
    # trusted on. In the CURRENT structure it is DEFENSIVE rather than
    # load-bearing: the five paths that record a verdict operate on disjoint
    # sets (over-cap literals are outside `candidates`; the budget sweep only
    # covers indices not yet reached; the rest are inline and per-candidate).
    # Verified by mutation — removing the dedup leaves the suite green, because
    # no reachable input double-records today. It is kept because the old
    # structure DID double-count (a short-core literal was recorded in a
    # pre-pass and again by the budget sweep, reporting 3 skips for 2 literals),
    # and the next path added here would have no such guarantee. Stated as
    # defence rather than left looking like covered ground.
    _seen: set[str] = set()

    def _skip(literal: str, reason: str) -> None:
        if literal in _seen:
            return
        _seen.add(literal)
        if skipped is not None:
            skipped.append({"literal": literal, "reason": reason})

    def _done(literal: str) -> None:
        # Consults `_seen` like `_skip` does. The comment above describes this
        # as a general defence against a literal recorded twice; without the
        # early return it guarded only the skip side, and a mutation deleting
        # the `_seen.add` here changed nothing.
        if literal in _seen:
            return
        _seen.add(literal)
        if examined is not None:
            examined.append(literal)

    for lit in prose[limit:]:
        _skip(lit, f"over the {limit}-literal cap")

    findings: list[dict] = []
    for index, lit in enumerate(candidates):
        if _remaining(deadline) <= 0:
            # Running out of time makes the result QUIETER, not wrong, and a
            # quieter result is indistinguishable from a clean one unless it
            # says so. Observed live: the same changeset scored 8/8 and 7/8 on
            # consecutive acceptance runs purely because another process on the
            # box made one file exceed its budget.
            for rest in candidates[index:]:
                _skip(rest, "scan budget exhausted")
            break

        core = cores.get(lit, "")
        if len(core) < _MIN_CORE_LEN:
            _skip(lit, "no distinctive searchable text in source")
            continue

        survivors = []
        # A survivor can be the EDITED FILE ITSELF. `removed` is occurrence-count
        # based, so a literal that went 2 -> 1 here still has a live copy here,
        # and the sibling this scanner exists to find is sitting in the same file.
        # Decided from new_counts rather than by re-reading the path, because the
        # bytes on disk are a THIRD state -- the index, or a later edit -- and
        # need not match the new_source this call was given.
        if new_counts[lit] > 0:
            survivors.append(edited)

        # Why an incomplete search is tracked as a STRING rather than a bool:
        # the two causes below are the same shape -- part of the question went
        # unanswered -- and they must be dispositioned identically, which a
        # per-cause branch quietly failed to do. Whatever is PROVEN is still
        # reported; only the unanswered part is disclosed.
        search_incomplete = ""

        try:
            paths = _grep_candidates(core, repo_root, deadline)
        except GrepUnavailable as exc:
            if not survivors:
                # Nothing proven AND nothing searched. Reporting this literal
                # as clean would be the exact failure the tool exists to name.
                _skip(lit, f"could not search: {exc}")
                continue
            # A same-file survivor came from `new_counts`, not from the search,
            # so it is a DEFINITE finding that the search failing cannot undo.
            # Only the repository-wide half went unanswered. Discarding a proven
            # finding because a later step failed is the mirror image of
            # reporting an unproven clean.
            paths = []
            search_incomplete = f"repository search failed: {exc}"

        truncated = False
        for path in paths:
            # Checked per candidate, not just per literal: one common core can
            # match hundreds of files, and parsing them is what actually
            # overruns.
            if _remaining(deadline) <= 0:
                # The outer loop records its truncation; this one used to fall
                # through to `if survivors:` and report a PARTIAL survivor list
                # as if it were complete. Same hazard, opposite direction.
                truncated = True
                break
            if path.resolve() == edited.resolve():
                continue
            try:
                # AST-confirm: the grep matched raw text, which can differ from
                # the literal's value wherever an escape is involved.
                if lit in string_literals(path.read_text(encoding="utf-8")):
                    survivors.append(path)
            except (OSError, UnicodeDecodeError) as exc:
                # THE THIRD WAY THE SEARCH COMES BACK PARTIAL, and the one an
                # earlier pass missed while claiming the set was closed. `git
                # grep` positively identified this file as containing the core;
                # the read could not confirm whether the literal is really
                # there. That question is unanswered in exactly the sense the
                # two causes above are -- and falling through to `_done` here
                # reported the literal as examined-CLEAN on evidence never
                # gathered, which is the failure this tool exists to name in
                # other people's code. Reachable without an exotic encoding:
                # `--untracked` means git grep can list a path that a
                # concurrent editor or build step removes before the read.
                search_incomplete = search_incomplete or (
                    f"could not read a candidate sibling: {exc}"
                )
                continue

        if truncated:
            # Same disposition as a failed grep: it is the other way the search
            # can come back partial, and it used to report its survivors with
            # no hint that more files went unexamined.
            search_incomplete = search_incomplete or "survivor scan truncated by budget"

        if survivors:
            finding = {"literal": lit, "survivors": survivors}
            if search_incomplete:
                finding["search_incomplete"] = search_incomplete
            findings.append(finding)
            _done(lit)
        elif search_incomplete:
            _skip(lit, search_incomplete)
        else:
            _done(lit)

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
    the same method name (``run``, ``main``, ``_render``, ``__init__``), and a
    last-one-wins map then compares one class's function against the other's.
    Keying by qualified name makes each comparison per-definition rather than
    per-bare-name.
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
    for node in _own_scope_nodes(fn):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            # ALL targets, not just a lone one: `rows = alias = get_all(db)`
            # binds both names to the same call.
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):  # incl. walrus
            targets = [node.target]
        if not targets:
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        callee = _callee_identity(value.func)
        if not callee:
            continue
        for target in targets:
            # Recurse through unpacking: `rows, meta = get_all(db)` binds both.
            for name in _name_targets(target):
                sources.setdefault(name, set()).add(callee)
    return sources


def _own_scope_nodes(fn: ast.AST):
    """Nodes in *fn*'s OWN scope, not descending into nested scopes.

    ``ast.walk`` descends into nested functions and classes, which attributes an
    inner function's assignment to the outer one — so one inner change is
    reported twice, once per enclosing scope. An assignment inside
    ``outer.inner`` is ``inner``'s provenance, never ``outer``'s.

    Note this is the ASSIGNMENT policy. Uses deliberately use a different one
    (see ``_uses_of``): a closure that READS an outer variable is a genuine use
    of it, so applying this same boundary there would trade a false positive for
    a false negative.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _name_targets(target: ast.AST):
    """Every name an assignment target binds, through tuple/list unpacking."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _name_targets(target.value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _name_targets(element)


def _uses_of(fn: ast.AST, var: str) -> list[int]:
    """Lines where *var* is READ in *fn*, including closures that do not rebind it.

    Deliberately the OPPOSITE boundary from ``_own_scope_nodes``. A nested
    function reading the enclosing variable is a genuine use, and exactly the
    kind an edit forgets to revisit; excluding nested scopes here would trade a
    false positive for a false negative, which is the wrong direction for a
    detector whose entire purpose is finding what a change left behind.

    A nested scope that BINDS the name is the different case: that name is its
    own variable, so its reads are not uses of ours and the subtree is skipped.
    """
    lines: list[int] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                and _scope_binds(node, var)):
            continue
        if (isinstance(node, ast.Name) and node.id == var
                and isinstance(node.ctx, ast.Load)):
            lines.append(node.lineno)
        stack.extend(ast.iter_child_nodes(node))
    return sorted(lines)


def _scope_binds(scope: ast.AST, var: str) -> bool:
    """Does *scope* bind *var* itself — as a parameter, or by assigning it?"""
    args = getattr(scope, "args", None)
    if args is not None:
        named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if any(a.arg == var for a in named):
            return True
        if args.vararg is not None and args.vararg.arg == var:
            return True
        if args.kwarg is not None and args.kwarg.arg == var:
            return True
    for node in _own_scope_nodes(scope):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr,
                               ast.For, ast.AsyncFor)):
            targets = [node.target]
        if any(var in set(_name_targets(t)) for t in targets):
            return True
    return False


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
            uses = _uses_of(new_fn, var)
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


# -- CLI ----------------------------------------------------------------------
#
# This was two always-on PreToolUse HOOKS. It is now a script run deliberately,
# and that is not a packaging preference -- it is what the measurement chose.
# A 10s hook budget forced MAX_LITERALS_CHECKED=6, and that cap alone caused 95%
# of the scanner's measured misses: 57% recall as a hook versus 100% as a script
# over the same 47 known divergences mined from history. The hook was blindest
# exactly where the risk is highest, because a large refactor produces the most
# literal changes and the cap keeps only the longest few. Off the hook clock,
# none of that applies.
#
# Being a script also deletes a class of defect rather than fixing it. The
# commit-time hook had to work out which repository a commit would land in,
# which meant hand-parsing git's own argv (-C, --git-dir, subcommand position,
# and the content-selection modes -a/--only/--include/pathspec). That is the
# argv-to-effect mapping this repo's guidance forbids, and it drew 7 of the 19
# review findings by itself. A CLI is TOLD what to scan, so it never arises.


class GitError(RuntimeError):
    """A git command FAILED, as distinct from returning nothing.

    This distinction is the whole point. The previous version returned "" for
    both, and every caller reads "" as "nothing to do" -- so an unreadable ref,
    a mid-merge tree, or a path that does not exist at the base all surfaced as
    a clean scan. That is precisely the failure this tool exists to catch, one
    layer down in the tool itself: an empty answer and an unanswered question
    are not the same, and only one of them is good news.
    """


# How many PATH fields follow each --name-status letter. R (rename) and C
# (copy) carry a similarity score and two paths; everything else carries one.
# Enumerated from git's own documented set rather than the three letters that
# happened to appear in testing -- the missing ones (T type-change, U unmerged)
# are exactly what desynced the parser.
_STATUS_ARITY = {"A": 1, "C": 2, "D": 1, "M": 1, "R": 2, "T": 1, "U": 1,
                 "X": 1, "B": 1}


def _git(args: list[str], cwd: str) -> str:
    """Run git and return stdout. Raises GitError if the command fails."""
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=30)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise GitError(f"git {' '.join(args[:2])} could not run: {exc}") from exc
    if out.returncode != 0:
        detail = (out.stderr or "").strip().splitlines()
        raise GitError(f"git {' '.join(args[:2])} failed: "
                       f"{detail[0] if detail else f'exit {out.returncode}'}")
    return out.stdout


def _git_optional(args: list[str], cwd: str) -> str | None:
    """Run git where FAILURE IS MEANINGFUL, and return None for it.

    Used only where a non-zero exit answers the question rather than obscuring
    it -- chiefly `git show <ref>:<path>` for a file that did not exist at that
    ref, which is information, not an error.
    """
    try:
        return _git(args, cwd)
    except GitError:
        return None


def _changed_python_files(base: str, cwd: str, staged: bool) -> list[tuple[str, str]]:
    """``[(old_path, new_path)]`` for modified/renamed .py files.

    ``--name-status -z -M``, not ``--name-only``: for a renamed-AND-edited file
    ``--name-only`` yields only the DESTINATION, and asking the base ref for
    that path returns nothing -- so every such file is skipped in silence, and
    that is precisely the change most likely to have left siblings behind. The
    rename record carries both paths, so the old blob is read from the old one.
    ``-z`` because a path containing a space or a non-ASCII byte is otherwise
    split or quoted, and the real file is skipped just as silently.
    """
    args = ["diff", "--name-status", "-z", "-M", base]
    if staged:
        args.insert(1, "--cached")
    fields = [f for f in _git(args, cwd).split("\0") if f]
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        letter = fields[i][:1]
        if letter not in _STATUS_ARITY:
            # Do NOT resync by guessing. The previous version advanced a single
            # field on an unknown status, which lands on a PATH -- and a path
            # beginning with M, A or R is then read as a status, desyncing the
            # rest of the record and dropping real files in silence. Reproduced
            # with a mid-merge `U` record. An unrecognised status means the
            # stream is no longer understood, so say so.
            raise GitError(
                f"unrecognised git status {fields[i]!r} in --name-status output; "
                "refusing to guess the record boundaries")
        npaths = _STATUS_ARITY[letter]
        if i + npaths >= len(fields):
            raise GitError(f"truncated --name-status record for status "
                           f"{fields[i]!r}")
        paths = fields[i + 1:i + 1 + npaths]
        i += 1 + npaths
        if letter in ("R", "C"):
            old, new = paths[0], paths[1]
        elif letter == "D":
            continue  # deleted: nothing survives to scan
        else:
            old = new = paths[0]
        if new.endswith(".py"):
            pairs.append((old, new))
    return pairs


# NO MOVE-SUPPRESSION -- tried, MEASURED, and REJECTED. Recorded because the
# idea is obviously right and is wrong.
#
# A refactor that relocates code moves its literals, so a "survivor" can be the
# literal's intended new home rather than a sibling anyone forgot. Suppressing
# any survivor whose file GAINED the literal in the same change looked like a
# free precision win, and on one large extraction refactor it took 96 findings
# down to a handful.
#
# Measured against both sides on the same day:
#
#     move-suppression ON   fire rate 6/100 commits,  acceptance 5/8
#     move-suppression OFF  fire rate 7/100 commits,  acceptance 8/8
#
# One point of precision for 37% of recall. The premise is simply false: a file
# can legitimately gain occurrences of a literal while STILL holding the stale
# copy that needed changing, so "gained it here" does not separate a move from a
# divergence. Three real divergences were suppressed as moves.
#
# This is the failure the acceptance bar exists to catch -- a filter that
# improves the number by breaking the thing it was built for -- and it was
# invisible in the precision measurement alone. If a large refactor ever
# produces unreadable output, tune --max-literals, which costs no recall.


def main(argv: list[str] | None = None) -> int:
    # Make stdout survivable BEFORE anything is printed.
    #
    # A static check keeps non-ASCII out of this file's own `print` calls, but
    # that is only half the class: the output also INTERPOLATES scanned data --
    # file paths, git error text, and literal values via `!r`, which in Python 3
    # does not escape printable non-ASCII. So a scanned file containing ordinary
    # accented prose emits non-ASCII regardless of what this source contains.
    # Under a non-UTF-8 stdout encoding that raises BETWEEN a finding and the
    # summary line, and a partial run prints as a complete one -- the single
    # failure this whole tool exists to prevent.
    #
    # `backslashreplace` rather than `replace`: an escaped literal is still
    # greppable and still identifies the sibling, where U+FFFD would not.
    for _stream in (sys.stdout, sys.stderr):
        # Suppressed, not handled: a stream that is not a reconfigurable
        # TextIOWrapper (a pytest capture object, a pipe wrapper) has nothing
        # to harden, and the scan must never fail over its own output setup.
        with contextlib.suppress(AttributeError, ValueError):
            _stream.reconfigure(errors="backslashreplace")

    parser = argparse.ArgumentParser(
        prog="class_scope_scan",
        description="Find changes that fixed one instance and left siblings behind.",
    )
    parser.add_argument("paths", nargs="*", help="restrict the scan to these files")
    parser.add_argument("--base", default="HEAD",
                        help="compare against this ref (default: HEAD)")
    parser.add_argument("--staged", action="store_true",
                        help="scan the index rather than the working tree")
    parser.add_argument("--max-literals", type=int, default=MAX_LITERALS_CHECKED,
                        help=f"literals checked per file (default {MAX_LITERALS_CHECKED})")
    parser.add_argument("--budget", type=float, default=SCAN_BUDGET_SECONDS,
                        help=f"seconds per file (default {SCAN_BUDGET_SECONDS})")
    args = parser.parse_args(argv)
    # Both bounds are validated HERE, at parse time, because both are used as
    # slice indices or deadline arithmetic downstream where a negative value
    # does not fail -- it quietly inverts the meaning. `--max-literals -1`
    # checked EVERY literal except the last and then reported that last one as
    # "over the -1-literal cap": the cap stopped bounding the work while the
    # output still claimed a cap had been applied. A budget <= 0 makes the
    # deadline already past, so every file reports as truncated.
    if args.max_literals < 1:
        parser.error("--max-literals must be at least 1")
    if args.budget <= 0:
        parser.error("--budget must be greater than 0")

    try:
        cwd = _git(["rev-parse", "--show-toplevel"], ".").strip()
    except GitError as exc:
        print(f"not inside a usable git repository: {exc}", file=sys.stderr)
        return 2
    repo = Path(cwd)

    try:
        pairs = _changed_python_files(args.base, cwd, args.staged)
    except GitError as exc:
        # An unreadable base ref, a mid-merge tree, or an unparseable record
        # used to print "no modified Python files" and exit 0 -- a scanner
        # reporting a clean result for a question it never got to ask.
        print(f"cannot determine what changed against {args.base!r}: {exc}",
              file=sys.stderr)
        return 2
    if args.paths:
        wanted = {str(Path(p).resolve()) for p in args.paths}
        pairs = [(o, n) for o, n in pairs if str((repo / n).resolve()) in wanted]
    if not pairs:
        print(f"no modified Python files against {args.base}")
        return 0

    findings = skipped_all = 0
    unreadable: list[str] = []
    for old_path, new_path in pairs:
        # _git_optional, not _git: a file absent at the base ref is INFORMATION
        # (it is new, or a rename git did not pair), not a failure. But it is
        # also not "scanned" -- it is counted separately so the summary line
        # cannot claim coverage it does not have.
        old_src = _git_optional(["show", f"{args.base}:{old_path}"], cwd)
        if old_src is None:
            unreadable.append(f"{new_path} (no version at {args.base})")
            continue
        if args.staged:
            new_src = _git_optional(["show", f":{new_path}"], cwd)
        else:
            try:
                new_src = (repo / new_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                unreadable.append(f"{new_path} ({exc.__class__.__name__})")
                continue
        if not new_src:
            unreadable.append(f"{new_path} (empty or unreadable at head)")
            continue

        skipped: list[dict] = []
        orphans = find_orphaned_literals(
            repo / new_path, old_src, new_src, repo,
            budget_seconds=args.budget, max_literals=args.max_literals,
            skipped=skipped,
        )
        for f in orphans:
            survivors = [str(s.relative_to(repo)) for s in f["survivors"]]
            findings += 1
            print(f"\n{new_path}")
            print(f"  changed {f['literal'].strip()[:70]!r}")
            print(f"  same text still in: {', '.join(survivors[:5])}")
            if f.get("search_incomplete"):
                # A finding from a partial search is still a finding, but the
                # ABSENCE of further survivors was never established -- say so
                # rather than let the reader infer a complete answer.
                # ASCII only: this was the sole non-ASCII `print` in the
                # file, and under a non-UTF-8 PYTHONIOENCODING it raised
                # BETWEEN the finding and its caveat -- killing the remaining
                # files and the summary line, so a partial run printed as a
                # complete one. On the code path added to stop exactly that.
                print(f"  (search incomplete: {f['search_incomplete']} -- "
                      f"there may be more)")
        for f in find_unrevisited_uses(old_src, new_src):
            findings += 1
            print(f"\n{new_path}")
            print(f"  `{f['variable']}` in {f['function']}() now comes from "
                  f"{f['now']}() not {f['was']}()")
            print(f"  uses untouched by this change, at lines: "
                  f"{', '.join(str(n) for n in f['unrevisited'])}")
        skipped_all += len(skipped)
        for s in skipped[:3]:
            print(f"\n{new_path}\n  NOT CHECKED ({s['reason']}): "
                  f"{s['literal'].strip()[:60]!r}", file=sys.stderr)

    # Always state the denominator. "No findings" from a scanner that examined
    # nothing is indistinguishable from a genuinely clean result, and this
    # scanner has been both -- so it reports what it LOOKED AT, not only what it
    # found. 95% of its measured misses were literals it never opened.
    scanned = len(pairs) - len(unreadable)
    print(f"\n{scanned} of {len(pairs)} changed file(s) scanned against "
          f"{args.base}; {findings} finding(s); "
          f"{skipped_all} literal(s) not checked.")
    for entry in unreadable:
        print(f"  NOT SCANNED: {entry}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
