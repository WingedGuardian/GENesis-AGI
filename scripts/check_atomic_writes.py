#!/usr/bin/env python3
"""CI guard: an atomic write must not orphan its temp file on the exception path.

THE CLASS. The shape is "write to a temp, then rename it into place". When the
write or the rename raises and nothing unlinks the temp, the temp survives. That
is not theoretical: `~/.genesis/tmp9cis_fly.tmp` sat on the origin install for
3.5 months, 0 bytes, with `mkstemp`'s default naming signature. One file proves
both halves -- the leak happens, and nothing sweeps that directory
(`disk_hygiene.sh` roots every find at a named SUBdirectory; `tmp_watchgod.sh`
covers `~/.genesis/cc-tmp` and `/tmp`. Neither covers the `~/.genesis` root).

WHY A GUARD AND NOT JUST FIXES. MEASURED 2026-09-07: 58 atomic-write sites across
51 files, 31 of them dirty. Fixing 31 instances of a recurring pattern leaves
nothing to stop instance 32. This is the prose-to-gate move: the rule was "clean
up your temp", carried by convention, and conventions are what reviewers find one
instance of at a time.

WHAT THIS GUARD CLAIMS, PRECISELY: a temp created IN THIS FUNCTION and renamed
into place, whose exception path does not unlink it. The "created in this
function" half is load-bearing and was learned expensively. A first version
anchored on the verb alone, and `rename`/`replace` also covers move-aside,
claim-by-rename, rotate and quarantine -- shapes whose first operand is DURABLE.
That shipped a 49-row ledger at 67% precision, and because this guard PRINTS a
remediation, 16 of those rows were booby-trapped work items: following "unlink
the temp" would have deleted a live credential (guardian/cred_integrity.py), a
user's file (dashboard/routes/files.py), pending telemetry on its restore path
(observability/span_ingest.py), and a corrupt entry quarantined as evidence
(guardian/alert/queue.py). A false row in a debt ledger is worse than noise.

POLARITY IS ALLOWLIST. A site that is neither clean nor baselined FAILS. A new
atomic write added next year is caught by construction -- WITH ONE STATED
EXCEPTION. The baseline key excludes the line number on purpose (line numbers
churn on every unrelated edit above them), so a SECOND unprotected write added
inside an already-baselined function, with the same temp name, collides with the
existing row and passes silently. Verified: no duplicate dirty keys exist today,
so the ledger is currently honest. But "instance 32 cannot arrive silently" is
true only OUTSIDE the 31 functions already listed, and saying it unqualified was
an overclaim. The baseline below is a
DEBT LEDGER, not an exemption list: every entry is a known leak awaiting a fix,
it is expected to shrink, and the guard reports entries that no longer match so
a landed fix cannot leave a stale row behind.

WHAT THIS GUARD CANNOT SEE, stated rather than discovered later:
  * Shell scripts. `guardian-gateway.sh` and friends need `trap 'rm -f "$tmp"'`
    and are not parsed here.
  * A cleanup performed by a helper this file calls (`self._cleanup()`), which
    reads as a leak. Baseline it with the reason; do not widen the detector to
    guess, because a detector that accepts an unexamined indirection accepts
    everything.
  * Whether the destination directory is swept. A leaked temp under `/tmp` is
    collected by the OS; one under `~/.genesis` is not. The guard treats them
    alike -- prioritisation belongs to the human reading the report.

ITS OWN DEFECTS, KEPT BECAUSE THEY GENERALISE. Three earlier versions were wrong,
in different directions, and every one of them LOOKED right:
  1. Anchored on `mkstemp`, which missed every hand-rolled temp name
     (`f"{path}.{os.getpid()}.tmp"`) -- including two of the known leaks -- and
     accepted "a cleanup verb appears somewhere in the function", where `close`
     from `os.fdopen` is present in nearly all of them. It called 5 of 6 known
     leaks SAFE.
  2. Anchored on `replace`/`rename` by arity, which admitted 16
     `dataclasses.replace(record, field=...)` calls as filesystem writes, and
     read the DESTINATION as the temp for the `tmp.replace(dest)` form -- so it
     checked the wrong operand for cleanup.
  3. Anchored on the verb but not the operand's ORIGIN, admitting the durable-
     operand shapes above; and it excluded only the BARE `replace(x, ...)` while
     the `dataclasses.replace(rec, f=1)` attribute form -- the one this repo
     actually uses, 4 sites -- sailed through into the baseline.
None was caught by running it. They were caught by SAMPLING the output, by
enumerating the shipped ledger rather than trusting its count, and by controls
that must flip. Tightening then LOST a real leak whose temp is bound two hops
away (`with NamedTemporaryFile(...) as tmp` -> `tmp_path = Path(tmp.name)`),
which is why `_born_in` is transitive: a precision fix measured without recall is
half a measurement. Hence `tests/test_scripts/test_check_atomic_writes.py`
pins known-clean AND known-leaking sites in both directions: a detector that
finds nothing is indistinguishable from one that looks at nothing.
"""

from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SKIP = ("tests/", ".claude/worktrees/", "node_modules/", "/.venv/")


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparseable>"


def _qualname(tree: ast.AST, node: ast.AST) -> str:
    """`Class.method` rather than `method`.

    The baseline key is (file, func, temp), and a BARE name lets two same-named
    methods in one file share one row -- so a baselined clean `A._write` would
    absorb a genuinely new leaking `C._write` and the guard would exit 0. Zero
    live collisions today; `_write::tmp` and `_atomic_write_json::tmp` already
    repeat across files, and `tmp` is the temp name in most rows, so it is a
    matter of time rather than of luck."""
    parts: list[tuple[int, str]] = []
    for anc in ast.walk(tree):
        if (
            isinstance(anc, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and anc is not node
            and anc.lineno <= node.lineno <= (anc.end_lineno or 0)
        ):
            parts.append((anc.lineno, anc.name))
    parts.sort()
    return ".".join([n for _, n in parts] + [node.name])


def _enclosing_func(tree: ast.AST, lineno: int):
    """The INNERMOST function containing lineno, so a nested helper's own
    try/except is not credited to its parent."""
    best = None
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.lineno <= lineno <= (n.end_lineno or n.lineno) and (
            best is None or n.lineno > best.lineno
        ):
            best = n
    return best


#: An assignment RHS that creates a scratch file. A durable path is never built
#: from these, which is what makes the test a shape rather than a guess.
_TEMP_MAKERS = ("mkstemp", "NamedTemporaryFile", "mkdtemp")
_TEMP_SUFFIXES = (".tmp", ".new", ".partial", ".part", ".swp", ".writing")


def _born_in(func: ast.AST, temp_expr: str) -> bool:
    """Is ``temp_expr`` bound in this function to something that MAKES a temp?

    Deliberately conservative, and TRANSITIVE, because the real pattern is two
    hops and a one-hop check silently loses it. MEASURED: an earlier one-hop
    version dropped `autonomy/cli_policy.py`, a known real leak, whose temp is
    created by `with tempfile.NamedTemporaryFile(...) as tmp:` and then renamed
    via `tmp_path = Path(tmp.name)`. Neither the `with` binding nor the derived
    name matches a "temp maker" on its own.

    So: seed from every name bound to a temp-maker (including `with ... as X`)
    or to an expression carrying a scratch suffix, then close over assignments
    whose right-hand side mentions a name already known to be a temp. Anything
    still unreached -- a parameter, a field, a path built from user input -- is
    not a temp, and the site is not this guard's business.
    """
    if not temp_expr:
        return False
    root = temp_expr.split(".")[0].split("[")[0].strip()

    def _bound_names(target) -> list[str]:
        return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]

    temps: set[str] = set()
    # Seed: `with tempfile.NamedTemporaryFile(...) as tmp:` and friends.
    for n in ast.walk(func):
        if (
            isinstance(n, ast.withitem)
            and n.optional_vars is not None
            and any(m in _unparse(n.context_expr) for m in _TEMP_MAKERS)
        ):
            temps.update(_bound_names(n.optional_vars))

    # Assignments, iterated to a fixpoint so a derived name is reached.
    pairs: list[tuple[list[str], ast.AST]] = []
    for n in ast.walk(func):
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value:
            targets, value = [n.target], n.value
        else:
            continue
        names = [nm for t in targets for nm in _bound_names(t)]
        if names:
            pairs.append((names, value))

    def _makes_temp(value: ast.AST) -> bool:
        """A temp-maker call, or a STRING LITERAL ending in a scratch suffix.

        Anchored to the literal's END rather than matched anywhere in the
        unparsed text: `path.with_suffix(".tmp")` makes a temp, while a variable
        merely named `tmp_dir_listing` does not, and a substring test cannot tell
        them apart."""
        for n in ast.walk(value):
            if isinstance(n, ast.Call):
                fn = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if fn in _TEMP_MAKERS:
                    return True
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value.endswith(_TEMP_SUFFIXES)):
                return True
        return False

    for names, value in pairs:
        if _makes_temp(value):
            temps.update(names)

    for _ in range(len(pairs) + 1):  # bounded: at most one new name per pass
        grew = False
        for names, value in pairs:
            # Propagate through the RHS's IDENTIFIERS, not its text: a name that
            # merely CONTAINS a known temp's name is a different variable.
            refs = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
            if (refs & temps) and not set(names) <= temps:
                temps.update(names)
                grew = True
        if not grew:
            break

    return root in temps


def _handlers_covering(func: ast.AST, lineno: int) -> list:
    """Every except-handler and finally-body whose TRY BODY contains lineno.

    Deliberately the try BODY, not the whole Try node: a rename sitting inside an
    `except:` block is not protected by that same handler."""
    out: list = []
    for n in ast.walk(func):
        if isinstance(n, ast.Try) and any(
            c.lineno <= lineno <= (c.end_lineno or c.lineno) for c in n.body
        ):
            out.extend(n.handlers)
            if n.finalbody:
                out.append(n.finalbody)
    return out


#: Wrappers that do not change WHICH path is meant, so `Path(tmp)`, `str(tmp)`
#: and `tmp.expanduser()` all still name `tmp`.
_TRANSPARENT = ("Path", "str", "os.fspath", "pathlib.Path")


def _strip_wrappers(expr: ast.AST) -> ast.AST:
    """Remove path-neutral wrappers: Path(x), str(x), x.expanduser()/resolve()."""
    node = expr
    for _ in range(8):  # bounded; nesting deeper than this is not real code
        if isinstance(node, ast.Call) and _unparse(node.func) in _TRANSPARENT and node.args:
            node = node.args[0]
            continue
        if isinstance(node, ast.Attribute) and node.attr in ("expanduser", "resolve", "absolute"):
            node = node.value
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("expanduser", "resolve", "absolute"):
            node = node.func.value
            continue
        break
    return node


def _core_name(expr: ast.AST) -> str:
    return _unparse(_strip_wrappers(expr))


def _alias_map(func: ast.AST) -> dict[str, ast.AST]:
    """Local single-assignment aliases, for resolving a reconstructed path.

    Cleanup often names the temp by REBUILDING it rather than reusing the
    variable: `tmp = path.with_suffix(".tmp")` written, then
    `Path(plan_path).expanduser().with_suffix(".tmp").unlink()` in the handler.
    That is genuinely clean, and identity matching alone reads it as a leak --
    so a false CLEAN became a false FLAG, which is better but still wrong.

    Only names assigned EXACTLY ONCE are resolved; a rebound name is ambiguous
    and is left alone rather than guessed at.
    """
    counts: dict[str, int] = {}
    values: dict[str, ast.AST] = {}
    for n in ast.walk(func):
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        t = n.targets[0]
        if isinstance(t, ast.Name):
            counts[t.id] = counts.get(t.id, 0) + 1
            values[t.id] = n.value
    return {k: v for k, v in values.items() if counts.get(k) == 1}


class _Substitute(ast.NodeTransformer):
    """Replace single-assignment local names with the expression they were bound to."""

    def __init__(self, aliases: dict[str, ast.AST]) -> None:
        self.aliases = aliases
        self.changed = False

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802 (ast API)
        repl = self.aliases.get(node.id)
        if repl is None:
            return node
        self.changed = True
        return copy.deepcopy(repl)


def _resolve(expr: ast.AST, aliases: dict[str, ast.AST], depth: int = 4) -> str:
    """Fully-substituted, wrapper-normalised form of an expression.

    Substitution is RECURSIVE, not root-only: the reconstructed cleanup path is
    `Path(plan_path).expanduser().with_suffix(".tmp")` while the temp is `tmp`,
    bound to `path.with_suffix(".tmp")` where `path` is itself a local. Resolving
    only the outermost name leaves `path.with_suffix('.tmp')` on one side and the
    fully-spelled form on the other, and they never meet.

    Bounded by `depth` and by single-assignment aliases only, so this cannot loop
    on a rebinding and cannot invent a resolution for an ambiguous name.
    """
    node = copy.deepcopy(expr)
    for _ in range(depth):
        sub = _Substitute(aliases)
        node = sub.visit(node)
        if not sub.changed:
            break
    return _unparse(_strip_wrappers(node))


def _unlinks(nodes: list, temp_expr: str, func: ast.AST | None = None) -> bool:
    """Does any handler unlink THE TEMP -- by identity, not by substring?

    Containment (`temp_expr in args`) was wrong in the direction that matters:
    MEASURED, `os.unlink(tmp_backup)` credited cleanup for temp `tmp`, so a
    genuinely leaking site read CLEANS_UP. A false CLEAN is strictly worse than a
    false flag here -- the leak is invisible AND excluded from the debt ledger,
    so nothing ever revisits it.
    """
    if not temp_expr:
        return False
    aliases = _alias_map(func) if func is not None else {}
    want_node = ast.parse(temp_expr, mode="eval").body
    wants = {_core_name(want_node), _resolve(want_node, aliases)}
    for grp in nodes:
        for item in (grp if isinstance(grp, list) else [grp]):
            for n in ast.walk(item):
                if not isinstance(n, ast.Call):
                    continue
                fn = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if fn not in ("unlink", "remove"):
                    continue
                targets: set[str] = set()
                for a in n.args:
                    targets.add(_core_name(a))
                    targets.add(_resolve(a, aliases))
                if isinstance(n.func, ast.Attribute):
                    targets.add(_core_name(n.func.value))
                    targets.add(_resolve(n.func.value, aliases))
                if wants & targets:
                    return True
    return False


def analyse_source(src: str, rel: str) -> list[dict]:
    """Every atomic-write site in one file, classified. Pure; no I/O."""
    tree = ast.parse(src)
    rows: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # A filesystem move is ALWAYS an attribute call. A bare `replace(x, ...)`
        # is dataclasses.replace -- the largest false-positive class this guard
        # had, and nothing about it touches a filesystem.
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("replace", "rename", "move"):
            continue
        owner = _unparse(node.func.value)
        # `shutil.move(src, dst)` is the same operation and was a straight blind
        # spot: MEASURED 5 live call sites, one of them three lines above a
        # baselined leak in the same function.
        if node.func.attr == "move" and owner != "shutil":
            continue
        # BOTH dataclasses forms have to be excluded, and only one of them was.
        # The bare `replace(x, ...)` (7 files import it) is excluded by the
        # Attribute test above. The ATTRIBUTE form `dataclasses.replace(rec, f=1)`
        # has exactly one positional arg, so arity does not separate it either --
        # MEASURED 4 live sites, all four of which reached the shipped baseline
        # as "atomic writes" with temp == "dataclasses".
        if owner in ("dataclasses", "dc"):
            continue
        # str.replace(old, new) takes >=2 args; Path.replace(target) exactly one;
        # os.replace(src, dst) two but is named unambiguously.
        if owner != "os" and node.func.attr != "move" and len(node.args) != 1:
            continue
        # A kwarg-only call (`os.replace(src=a, dst=b)`) has no positional args;
        # falling through would set temp = "os" and then credit ANY os.unlink in
        # a handler as cleanup.
        if owner in ("os", "shutil") and len(node.args) < 2:
            continue
        # WHICH OPERAND IS THE TEMP depends on the form. Getting this wrong
        # silently checks the DESTINATION for cleanup instead of the temp.
        # The receiver keeps its wrappers, and `Path(tmp).replace(dest)` is a
        # common house style here -- MEASURED invisible at ego/config.py:98,
        # mcp/health/settings.py:706 and outreach/config.py:231, which produced
        # NO ROW at all. `_born_in` looks up a bare NAME, and `Path(tmp_path)` is
        # not one, so the site was silently dropped rather than judged. Strip
        # first, then take the temp.
        temp = (
            _unparse(_strip_wrappers(node.args[0]))
            if owner in ("os", "shutil")
            else _unparse(_strip_wrappers(node.func.value))
        )
        func = _enclosing_func(tree, node.lineno)
        # THE OPERAND MUST BE A TEMP THIS FUNCTION CREATED. Anchoring on the verb
        # alone was wrong by a third: `rename`/`replace` also covers move-aside,
        # claim-by-rename, rotate and quarantine, where the first operand is
        # DURABLE. MEASURED on the first shipped baseline: 16 of 49 rows were
        # those shapes -- and this guard prints "unlink the temp" as the
        # remediation, so following it would have deleted a live credential
        # (guardian/cred_integrity.py), a user's file (dashboard/routes/files.py),
        # pending telemetry on its restore path (observability/span_ingest.py) and
        # a quarantined corrupt entry kept as evidence (guardian/alert/queue.py).
        # A false-positive row in a debt ledger is not noise; it is a booby-trapped
        # work item. Narrowing here TIGHTENS the allowlist rather than loosening
        # it: a temp created in a CALLER is not claimed, which is the documented
        # cross-function limitation, not a new hole.
        if func is not None and not _born_in(func, temp):
            continue
        if func is None:
            rows.append({"file": rel, "line": node.lineno, "func": "<module>",
                         "temp": temp, "verdict": "NO_HANDLER"})
            continue
        handlers = _handlers_covering(func, node.lineno)
        if not handlers:
            verdict = "NO_HANDLER"
        elif _unlinks(handlers, temp, func):
            verdict = "CLEANS_UP"
        else:
            verdict = "LEAKS"
        rows.append({"file": rel, "line": node.lineno,
                     "func": _qualname(tree, func), "temp": temp,
                     "verdict": verdict})
    return rows


def key(row: dict) -> str:
    """Baseline identity. Deliberately EXCLUDES the line number, which shifts on
    every unrelated edit above it and would turn the ledger into churn."""
    return f"{row['file']}::{row['func']}::{row['temp']}"


def scan(repo: Path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for base in ("src", "scripts"):
        for path in sorted((repo / base).rglob("*.py")):
            rel = str(path.relative_to(repo))
            if any(s in rel for s in _SKIP):
                continue
            try:
                rows.extend(analyse_source(path.read_text(encoding="utf-8"), rel))
            except (OSError, SyntaxError, ValueError) as exc:
                # Fail CLOSED: an unreadable file is not a clean file.
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
    return rows, errors


def main() -> int:
    baseline_path = REPO / "config" / "atomic_write_baseline.json"
    try:
        baseline = set(json.loads(baseline_path.read_text(encoding="utf-8"))["known"])
    except (OSError, ValueError, KeyError) as exc:
        print(f"FAIL: cannot read {baseline_path}: {exc}", file=sys.stderr)
        return 1

    rows, errors = scan(REPO)
    if errors:
        print("FAIL: files could not be analysed (a guard that cannot read a file "
              "must not report it clean):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    if len(rows) < len(baseline):
        # Path.rglob on a missing directory yields nothing rather than raising,
        # so a mis-rooted scan produced ([], []) -> no dirty sites -> exit 0, a
        # green check for a run that examined nothing. The floor is
        # self-maintaining: as the ledger shrinks, so does the threshold.
        print(f"FAIL: scanned only {len(rows)} sites against a {len(baseline)}-row "
              "baseline. A scan that sees fewer sites than its own ledger did not "
              "run -- check that src/ and scripts/ exist at the expected paths.",
              file=sys.stderr)
        return 1

    dirty = [r for r in rows if r["verdict"] in ("LEAKS", "NO_HANDLER")]
    new = [r for r in dirty if key(r) not in baseline]
    seen = {key(r) for r in dirty}
    stale = sorted(baseline - seen)

    clean = sum(1 for r in rows if r["verdict"] == "CLEANS_UP")
    print(f"atomic-write guard: {len(rows)} sites, {clean} clean, "
          f"{len(dirty)} known-dirty, {len(new)} NEW")

    if stale:
        print("\nBaseline entries that no longer match a dirty site. A fix landed "
              "-- remove these rows so the ledger keeps shrinking:")
        for s in stale:
            print(f"  {s}")
        print("\nFAIL: drop these rows in the same change that fixed them, or the "
              "ledger stops shrinking and the next reader cannot tell debt from "
              "noise.", file=sys.stderr)
        return 1

    if new:
        print("\nFAIL: new atomic write(s) with no cleanup on the exception path.",
              file=sys.stderr)
        for r in new:
            print(f"  {r['file']}:{r['line']} {r['func']}()  temp={r['temp']}  "
                  f"[{r['verdict']}]", file=sys.stderr)
        print(
            "\nFIRST confirm the first operand really is a scratch file this "
            "function created. `rename`/`replace` also covers move-aside, "
            "claim-by-rename, rotate and quarantine, where that operand is "
            "DURABLE and the correct fix is NOTHING -- unlinking it would destroy "
            "live data. Only once it is a temp: route the write through "
            "genesis.util.atomic.atomic_write_text, or unlink the temp in an "
            "except/finally. If it is not an atomic write at all, that is a "
            "detector bug worth fixing rather than a row worth adding to "
            f"{baseline_path.relative_to(REPO)}.", file=sys.stderr)
        return 1

    print("CLEAN: no new unguarded atomic writes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
