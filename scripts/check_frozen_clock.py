#!/usr/bin/env python3
"""Frozen-clock guard — no test may capture a wall clock at IMPORT time.

THE BUG CLASS THIS CLOSES (three recurrences: #978, #1441, and again in 2026-08).
A test materialises a timestamp into state under test; production then AGES that
timestamp against the LIVE clock with no injectable ``now``. The assertion holds only
while (elapsed since the capture) stays under (seed offset - production threshold). A
whole test suite takes many minutes to run, so a capture evaluated ONCE at import gives
the whole suite's runtime to burn that margin — and the failure surfaces on slow
runners only, which is how it survived twice.

Measured instance that motivated this guard: a pulse seeded 30 minutes in the future,
checked against a 5-minute future-skew tolerance, i.e. a 25-minute margin. It passed at
16m02s into a CI run and FAILED at 27m12s. Roughly 3% of runs (1 of 31 surveyed).

Syntax is NOT the key. Both earlier sweeps enumerated absolute ISO-date LITERALS and
each declared the class closed; a ``datetime.now()`` frozen at import walked straight
through both. So this guard keys on WHEN THE CLOCK IS READ, not on how a date is spelled.

WHAT IT FLAGS — a wall-clock call in a position evaluated ONCE, early:
  * module body          — the classic; one read for the entire session
  * class body           — same, at class-definition time
  * default argument     — evaluated at ``def`` time, not per call
  * decorator argument   — evaluated at import (e.g. inside ``@parametrize(...)``)
  * class/module/package/session-scoped fixture body — any scope broader than
    ``function`` shares ONE read across many tests

WHAT IT DOES NOT FLAG (deliberately — these read the clock at use time):
  * any call inside an ordinary function/method/lambda body, including function-scoped
    fixtures — that is the FIX this guard steers you toward — and the defaults and
    decorators of a ``def`` NESTED in one, which re-bind on every call.
  * a generator expression's element and its inner ``for`` clauses, which are lazy. Its
    LEFTMOST iterable is evaluated at creation and IS flagged. List/set/dict
    comprehensions are eager throughout and are flagged.
  * a ``skipif``/``xfail`` condition, whose contract IS import-time evaluation — in
    decorator position and equally in a module-level ``pytestmark`` assignment.
  * ``if __name__ == "__main__":`` blocks, which no suite run executes.

SCOPE IS ``tests/`` DELIBERATELY, and the reason is not that production is exempt: a
module-level clock in a long-running daemon is the worse version of this bug. It is
that in production such a capture is usually a legitimate process-start baseline
(``guardian/check.py`` holds one, correctly, for uptime), so the shape carries no
signal there, while under ``tests/`` it is nearly always the bug. A ``src/`` sweep run
when this guard was written found exactly that one site and nothing else.

KNOWN BLIND SPOTS, stated rather than implied:
  * an ALIASED import — ``from datetime import datetime as dt`` then ``dt.now()``, or
    ``from time import time`` then ``time()``. Matching is on the dotted call, so an
    alias is invisible. This is the blind spot a contributor is most likely to hit by
    accident; widening the match to any bare ``now()``/``time()`` would fire on
    unrelated APIs, which costs more than it buys.
  * a clock read inside a helper the module body then CALLS (``SEED = _make_seed()``).
    The value is still frozen at import, but proving it needs interprocedural analysis.
  * an absolute date LITERAL near a production threshold. A real bomb, but not
    statically decidable — it depends on a threshold this guard cannot know. NOT
    covered here, and no claim is made that the literal population is clean.

ESCAPE HATCH — ``# frozen-clock-ok: <why, WITH the measured margin>``, in a real
comment either inside the flagged statement's line span or in the contiguous comment
block directly above it. Both positions are accepted because a justification names a
margin AND the production threshold it is measured against, which does not fit in the
trailing columns of an already-long line under a 100-column limit. Three rules make the
hatch mean something:
  * the reason must be non-empty AND state a magnitude — a number, or the word
    "unbounded" for a site nothing ages. This is a SOCIAL guard, not a mechanical
    one: it forces the shape of a measurement, and cannot tell a real margin from a
    digit that happens to be in an issue number. It raises the cost of waving the
    guard off; it does not make that impossible;
  * only REAL comment tokens count, so a marker inside a string literal does not
    silence anything;
  * a span needs as many markers as it has distinct flagged calls, so one waiver cannot
    cover two sites — each site records its own measurement.
Of the five sites that existed when this guard was written, four were legitimate and
carry such a waiver; the fifth was the bug that motivated it.

A waiver attaches to the STATEMENT the clock is in. A marker above a fixture's
decorator therefore does not waive a clock in that fixture's BODY, and a marker above
a decorator does not waive a clock in a default argument — put it on the statement the
guard names in its output. That direction is fail-safe: the guard still fires.

Usage:  python scripts/check_frozen_clock.py   (exit 0 = clean, 1 = violation)
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

SCAN_ROOT = Path("tests")

# Dotted call suffixes that read a wall clock. Monotonic clocks are included: a
# module-level perf_counter/monotonic baseline drifts by the same mechanism.
CLOCK_SUFFIXES: tuple[str, ...] = (
    "datetime.now",
    "datetime.utcnow",
    "datetime.today",
    "date.today",
    "time.time",
    "time.time_ns",
    "time.monotonic",
    "time.monotonic_ns",
    "time.perf_counter",
    "time.perf_counter_ns",
    "time.process_time",
    "time.process_time_ns",
    "time.thread_time",
    "time.thread_time_ns",
    "time.localtime",
    "time.gmtime",
    "time.clock_gettime",
    "time.clock_gettime_ns",
)

# ``localtime(secs)``/``gmtime(secs)`` CONVERT a supplied epoch value; only their
# zero-argument forms read the current clock.
ARG_SENSITIVE: frozenset[str] = frozenset({"time.localtime", "time.gmtime"})

# Fixture scopes whose body is evaluated once, well before the tests that use it.
BROAD_SCOPES = frozenset({"session", "module", "package", "class"})

# Decorators whose arguments are import-time BY CONTRACT — flagging them is noise.
IMPORT_TIME_BY_DESIGN = frozenset({"skipif", "xfail"})

OPT_OUT = re.compile(r"#\s*frozen-clock-ok:\s*(?P<reason>\S.*)")
# A margin is a magnitude. "unbounded" is the honest form for a site nothing ages.
_STATES_A_MARGIN = re.compile(r"\d|unbounded", re.IGNORECASE)

# (lineno, col, dotted, shape, span_start, span_end)
Hit = tuple[int, int, str, str, int, int]
# (relpath, lineno, shape, dotted)
Violation = tuple[str, int, str, str]


def _dotted(node: ast.expr) -> str:
    """Best-effort dotted name for a call target (``datetime.datetime.now``)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_clock(dotted: str) -> bool:
    return any(dotted == s or dotted.endswith("." + s) for s in CLOCK_SUFFIXES)


def _terminal(dotted: str) -> str:
    """Last dotted component — ``pytest.mark.skipif`` -> ``skipif``.

    Matched exactly rather than by suffix: ``endswith("skipif")`` also accepts
    ``custom_skipif``, which would let an arbitrary decorator disarm the guard, and
    ``endswith("fixture")`` accepts ``not_a_fixture``, which would flag its body.
    """
    return dotted.rsplit(".", 1)[-1]


def _reads_the_clock(call: ast.Call) -> bool:
    """True if this call actually reads the current time."""
    dotted = _dotted(call.func)
    if not _is_clock(dotted):
        return False
    if any(dotted == s or dotted.endswith("." + s) for s in ARG_SENSITIVE):
        # gmtime(0) converts a supplied epoch; gmtime() reads the clock.
        return not (call.args or call.keywords)
    return True


def _eager_parts(node: ast.AST) -> list[ast.AST]:
    """The parts of a part-eager construct that are evaluated where it appears."""
    if isinstance(node, ast.Lambda):
        return [*node.args.defaults, *[d for d in node.args.kw_defaults if d]]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return []  # its defaults/decorators are visited with the function itself
    if isinstance(node, ast.GeneratorExp):
        return [node.generators[0].iter] if node.generators else []
    return [node]


def _eager_children(node: ast.AST) -> list[ast.AST]:
    """Children of ``node`` evaluated NOW, with the deferred parts pruned.

    Three constructs are part-eager and part-deferred, and treating any of them as
    wholly deferred loses a real import-time read:
      * a ``def``/``lambda`` BODY runs when called, but its DEFAULTS are evaluated
        where the definition appears;
      * a generator expression's element and inner ``for`` clauses are lazy, but its
        LEFTMOST iterable is consumed when the generator is created.
    """
    out: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        out.extend(_eager_parts(child))
    return out


def _clock_calls(node: ast.AST) -> list[ast.Call]:
    """Clock-reading calls in ``node`` that are evaluated when ``node`` is.

    The ROOT is checked as well as the children: a scoped fixture may hold a nested
    ``def`` whose body only runs when the returned callable is invoked.
    """
    found: list[ast.Call] = []
    # The ROOT obeys the same rules as any child — it can itself be a part-eager
    # construct (a lambda passed directly as a default, a bare generator
    # expression), and treating it specially is what made those cases wrong in
    # both directions.
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.GeneratorExp):
        stack: list[ast.AST] = _eager_parts(node)
    else:
        stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Call) and _reads_the_clock(cur):
            found.append(cur)
        stack.extend(_eager_children(cur))
    return found


def _fixture_scope(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The declared scope if ``fn`` is a pytest fixture, else None.

    A ``scope=`` that is not a literal (a name, an enum, ``**kwargs``) is treated as
    BROAD: the guard cannot prove it is narrow, and guessing "function" would fail open
    on exactly the shape it exists to catch.
    """
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if _terminal(_dotted(target)) != "fixture":
            continue
        if not isinstance(dec, ast.Call):
            return "function"
        for kw in dec.keywords:
            if kw.arg is None:  # **kwargs — scope unknowable
                return "session"
            if kw.arg == "scope":
                if isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
                return "session"  # fail closed
        return "function"
    return None


def _comment_lines(source: str) -> dict[int, str]:
    """{lineno: text} for REAL comment tokens — a '#' inside a string is not one."""
    out: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        pass  # a file we cannot tokenize simply has no usable waivers
    return out


def _markers_for_span(
    comments: dict[int, str], lines: list[str], start: int, end: int
) -> int:
    """How many well-formed waivers apply to the statement spanning start..end."""
    count = 0
    for lineno in range(start, end + 1):
        text = comments.get(lineno)
        if text and (m := OPT_OUT.search(text)) and _STATES_A_MARGIN.search(m.group("reason")):
            count += 1
    idx = start - 1  # contiguous comment-only block directly above the statement
    while idx >= 1 and idx <= len(lines) and lines[idx - 1].lstrip().startswith("#"):
        text = comments.get(idx)
        if text and (m := OPT_OUT.search(text)) and _STATES_A_MARGIN.search(m.group("reason")):
            count += 1
        idx -= 1
    return count


def _top_level_functions(tree: ast.AST):
    """Every function NOT nested inside another function.

    A `def` inside a test body re-executes on each call, so its defaults and
    decorators bind at call time — that is the very shape this guard steers people
    toward, and flagging it would charge a waiver for the fix.
    """
    out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def walk(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            is_fn = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            if is_fn and not inside_function:
                out.append(child)
            walk(child, inside_function or is_fn or isinstance(child, ast.Lambda))

    walk(tree, False)
    return out


def _own_yields(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    """Yield expressions belonging to ``fn`` ITSELF, not to a nested function.

    A ``yield`` inside a helper defined in the fixture says nothing about where the
    fixture's own setup ends.
    """
    found: list[ast.expr] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if isinstance(child, ast.Yield | ast.YieldFrom):
                found.append(child)
            walk(child)

    walk(fn)
    return found


def _fixture_setup(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """The statements of a fixture that run during SETUP.

    A generator fixture's statements after its own ``yield`` run at TEARDOWN, once
    every consumer has finished, so a clock read there cannot age before use. This
    follows the fixture's execution FLOW rather than stopping at whichever statement
    happens to contain a yield: a ``yield`` wrapped in ``try`` must not drag the
    ``finally`` cleanup into setup, and a yield inside a nested helper must not cut
    setup short and let a genuinely shared clock through.
    """
    yields = _own_yields(fn)
    if not yields:
        return list(fn.body)  # a return-style fixture: the whole body is setup
    first = min(y.lineno for y in yields)
    setup: list[ast.stmt] = []

    def collect(body: list[ast.stmt]) -> None:
        for st in body:
            if st.lineno > first:
                return  # everything from here on runs after the yield
            if (getattr(st, "end_lineno", None) or st.lineno) < first:
                setup.append(st)
                continue
            # This statement SPANS the yield. Descend, so that a `finally` (which
            # runs at teardown) is not swept in with the setup blocks.
            blocks = [getattr(st, "body", None), getattr(st, "orelse", None)]
            handlers = getattr(st, "handlers", None) or []
            if not any(blocks) and not handlers:
                setup.append(st)  # e.g. `yield expr` — the operand IS setup
                continue
            for block in blocks:
                if block:
                    collect(block)
            for handler in handlers:
                collect(handler.body)
            # `finalbody` is deliberately NOT collected — it runs at teardown.

    collect(fn.body)
    return setup


def _is_dunder_main(node: ast.stmt) -> bool:
    """Exactly ``if __name__ == "__main__":`` — never executed under pytest.

    The comparison operator and the literal both matter: ``if __name__ !=
    "__main__":`` and ``if __name__ == "some_module":`` DO run on import, so a
    looser predicate would wave a real frozen clock straight through.
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _under_import_time_contract(node: ast.AST) -> set[int]:
    """Column-tagged clock calls inside a skipif/xfail condition.

    Both evaluate their condition at import BY CONTRACT, in decorator position and
    equally in a module-level ``pytestmark`` assignment, so no seed can age there.
    """
    exempt: set[int] = set()
    for sub_node in ast.walk(node):
        if not isinstance(sub_node, ast.Call):
            continue
        target = sub_node.func
        if _terminal(_dotted(target)) in IMPORT_TIME_BY_DESIGN:
            for inner in ast.walk(sub_node):
                if isinstance(inner, ast.Call) and _reads_the_clock(inner):
                    exempt.add(id(inner))
    return exempt


def _enclosing_statements(tree: ast.AST) -> dict[int, ast.stmt]:
    """id(node) -> the INNERMOST statement containing it.

    A waiver binds to the statement its clock actually sits in. Keying to an outer
    compound statement instead would pool every clock in an ``if``/``try`` block into
    one span, where a stale marker left beside a deleted clock would silently exempt a
    different clock added elsewhere in the same block.
    """
    owner: dict[int, ast.stmt] = {}

    def walk(node: ast.AST, current: ast.stmt | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.stmt) else current
            if inner is not None:
                owner[id(child)] = inner
            walk(child, inner)

    walk(tree, None)
    return owner


def _annotations_are_deferred(tree: ast.Module) -> bool:
    """True if ``from __future__ import annotations`` is active in this module.

    With it, every annotation is stored as a STRING and never evaluated, so a clock
    written in one cannot freeze anything. Without it (the language default on 3.12),
    a ``def`` annotation is evaluated when the ``def`` executes.
    """
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__" and any(
            alias.name == "annotations" for alias in stmt.names
        ):
            return True
    return False


def _strip_annotations(node: ast.AST) -> None:
    """Blank every annotation in ``node`` — they are strings, not evaluated code."""
    for sub_node in ast.walk(node):
        if isinstance(sub_node, ast.AnnAssign | ast.arg):
            sub_node.annotation = None
        elif isinstance(sub_node, ast.FunctionDef | ast.AsyncFunctionDef):
            sub_node.returns = None


def scan_source(source: str, rel: str) -> list[Violation]:
    """Return [(relpath, lineno, shape, dotted)] for un-waived frozen clocks."""
    tree = ast.parse(source)
    deferred_annotations = _annotations_are_deferred(tree)
    if deferred_annotations:
        # Under future-annotations they are never evaluated, so reporting a clock
        # inside one would be a false positive on most of this repo's test files.
        _strip_annotations(tree)
    # split("\n"), NOT splitlines(): the latter also breaks on NEL, VT, FF and the
    # Unicode line separators, which Python's tokenizer does not count as physical
    # lines — one such character inside a string literal would shift every waiver
    # lookup after it and drop a valid waiver.
    lines = source.split("\n")
    comments = _comment_lines(source)
    owner = _enclosing_statements(tree)
    exempt_calls = _under_import_time_contract(tree)
    hits: dict[tuple[int, int, str], Hit] = {}

    def span_of(node: ast.AST) -> tuple[int, int]:
        return (node.lineno, getattr(node, "end_lineno", None) or node.lineno)

    def record(node: ast.AST, shape: str, span: tuple[int, int] | None = None) -> None:
        for call in _clock_calls(node):
            if id(call) in exempt_calls:
                continue
            # Bind to the innermost statement holding THIS call, not to the outer
            # construct we happened to start the walk from.
            here = span if span is not None else span_of(owner.get(id(call), call))
            key = (call.lineno, call.col_offset, _dotted(call.func))
            hits.setdefault(key, (*key, shape, *here))

    def walk_import_time(body: list[ast.stmt], shape: str) -> None:
        """Statements evaluated when the module is imported."""
        for st in body:
            if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef):
                continue  # handled below; only its defaults/decorators run now
            if _is_dunder_main(st):
                continue  # only runs when the file is executed directly, never in a suite
            if isinstance(st, ast.ClassDef):
                for extra in [*st.decorator_list, *st.bases, *st.keywords]:
                    record(extra, shape, span_of(extra))
                walk_import_time(st.body, "class-body")
                continue
            record(st, shape)

    walk_import_time(tree.body, "module-body")

    for node in _top_level_functions(tree):
        # The signature region only — a waiver in the body must not cover a default.
        sig_end = node.body[0].lineno - 1 if node.body else node.lineno
        sig_span = (node.lineno, max(node.lineno, sig_end))
        for default in [*node.args.defaults, *[k for k in node.args.kw_defaults if k]]:
            record(default, "default-arg", sig_span)
        for dec in node.decorator_list:
            record(dec, "decorator-arg", span_of(dec))
        if not deferred_annotations:
            # Without future-annotations, a signature annotation is evaluated when the
            # `def` executes — the same import-time moment as a default.
            args = node.args
            every = [*args.posonlyargs, *args.args, *args.kwonlyargs,
                     args.vararg, args.kwarg]
            for arg in every:
                if arg is not None and arg.annotation is not None:
                    record(arg.annotation, "annotation", sig_span)
            if node.returns is not None:
                record(node.returns, "annotation", sig_span)
        if (_fixture_scope(node) or "function") in BROAD_SCOPES:
            for st in _fixture_setup(node):
                record(st, "scoped-fixture")

    by_span: dict[tuple[int, int], list[Hit]] = {}
    for hit in hits.values():
        by_span.setdefault((hit[4], hit[5]), []).append(hit)

    out: list[Violation] = []
    for (start_line, end_line), group in by_span.items():
        # One recorded measurement per site: N calls in a span need N waivers.
        waived = _markers_for_span(comments, lines, start_line, end_line)
        for hit in sorted(group)[waived:]:
            out.append((rel, hit[0], hit[3], hit[2]))
    return sorted(out, key=lambda v: (v[0], v[1]))


def _walk_py(root: Path) -> tuple[list[Path], list[tuple[Path, OSError]]]:
    """Every .py under root, plus the directories that could not be listed.

    ``Path.rglob`` swallows an unlistable directory, which would hide every file
    beneath it — the same silent pass the read/parse branches refuse.
    """
    files: list[Path] = []
    bad: list[tuple[Path, OSError]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError as exc:
            bad.append((current, exc))
            continue
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink():
                stack.append(entry)
            elif entry.suffix == ".py":
                files.append(entry)
    return sorted(files), bad


def scan(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    files, unlistable = _walk_py(root)
    for directory, exc in unlistable:
        print(f"::error::frozen-clock guard could not list {directory}: {exc}")
        violations.append((directory.as_posix(), 0, "unreadable-dir", "-"))
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # A file the guard cannot READ is exactly as much of a gap as one it
            # cannot PARSE. Never a silent pass — that would print CLEAN over a bomb.
            print(f"::error::frozen-clock guard could not read {path}: {exc}")
            violations.append((path.as_posix(), 0, "unreadable", "-"))
            continue
        try:
            violations.extend(scan_source(source, path.as_posix()))
        except SyntaxError as exc:
            print(f"::error::frozen-clock guard could not parse {path}: {exc}")
            violations.append((path.as_posix(), exc.lineno or 0, "unparseable", "-"))
    return violations


def main() -> int:
    if not SCAN_ROOT.is_dir():
        print(f"frozen-clock guard: scan root {SCAN_ROOT} not found (run from repo root)")
        return 1
    violations = scan(SCAN_ROOT)
    if not violations:
        print("Frozen-clock guard: CLEAN (no import-time wall-clock captures under tests/)")
        return 0
    print("::error::A test captures the wall clock at import time (frozen-clock bomb).")
    print("Read the clock where it is USED (inside the test or its helper), so the")
    print("margin is one test's duration instead of the whole suite's runtime.")
    print("If the site is genuinely safe, put this in a comment on the statement or")
    print("directly above it (one per flagged call):")
    print("    # frozen-clock-ok: <why, with the measured margin>")
    print("A session-scoped fixture that IS a start-time baseline is legitimate — its")
    print("waiver is `unbounded — nothing ages this seed`, not a made-up number.")
    for rel, lineno, shape, dotted in violations:
        print(f"  {rel}:{lineno}: [{shape}] {dotted}()")
    return 1


if __name__ == "__main__":
    sys.exit(main())
