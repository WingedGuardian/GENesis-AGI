#!/usr/bin/env python3
"""CI guard: an atomic write must not orphan its temp file on the exception path.

THE CLASS. The shape is "write to a temp, then rename it into place". When the
write or the rename raises and nothing unlinks the temp, the temp survives. That
is not theoretical: `~/.genesis/tmp9cis_fly.tmp` sat on the origin install for
3.5 months, 0 bytes, with `mkstemp`'s default naming signature. One file proves
both halves -- the leak happens, and nothing sweeps that directory
(`disk_hygiene.sh` roots every find at a named SUBdirectory; `tmp_watchgod.sh`
covers `~/.genesis/cc-tmp` and `/tmp`. Neither covers the `~/.genesis` root).

WHY A GUARD AND NOT JUST FIXES. MEASURED 2026-09-09 against the merge of this
branch into main: 60 atomic-write sites across 52 files, 30 of them dirty.
That denominator moved THREE times, in both directions, and every move is worth
recording because each was invisible in a different way:
  * +1 site (58 -> 59). The temp-name test was anchored to the END of a string
    literal, so `f".{name}.restore-tmp-{getpid()}"` at guardian/cred_integrity.py
    produced NO ROW at all. The site was invisible rather than misjudged, which
    is why the error surfaced in the DENOMINATOR and in no verdict. It reads
    CLEANS_UP, so the dirty count was unaffected.
  * -1 site (59 -> 58, and dirty 31 -> 30). While this branch was open, main
    #1609 rewrote git_discard_guard._write_log_row to one file per flush,
    deleting the size-trim that renamed a temp into place. Its baseline row went
    stale and THIS GUARD FAILED CI until the row was dropped -- the shrink
    mechanism working on a fix it did not author. Note the counts must be
    re-derived against the MERGE, not the branch: a fix landing on main while a
    PR is open makes the PR's ledger stale, by design.
  * +2 sites (58 -> 60), found by an adversarial audit, both CLEANS_UP. Publish
    by HARDLINK -- `os.link(staging, final)` -- was not in the verb set, and in
    one of the two the marker lives in a MODULE CONSTANT, so
    `f"{stem}{_STAGING_SUFFIX}"` carries no ast.Constant piece for any suffix
    test to read. Both sites were absent, not misjudged. Note where the first
    one is: scripts/hooks/audit_jsonl.py, the file main added in the same window
    and the file that ABSORBED the row dropped above. MEASURED: with its two
    exception-path unlinks deleted, the guard reported `0 NEW` and exited 0.
Fixing 30 instances of a recurring pattern leaves nothing to stop instance 31.
This is the prose-to-gate move: the rule was "clean up your temp", carried by
convention, and conventions are what reviewers find one instance of at a time.

WHAT THIS GUARD CLAIMS, PRECISELY: a temp created IN THIS SCOPE, written, and
renamed into place, where some reachable path out leaves it on disk. Three parts
of that sentence were narrower until a review round widened them, and each was
wrong in a way worth stating:

  * SCOPE, not function. Module level is a scope too. Skipping the born-here
    test there and stamping NO_HANDLER made a module-level move-aside produce a
    dirty row for a DURABLE operand while a module-level write that DOES clean up
    still read dirty.
  * WRITTEN, not just renamed. A handler wrapped around only the rename leaves
    the temp when the WRITE fails, which is the class this guard is named for.
    Creation is deliberately NOT the trigger -- if `mkstemp` itself fails there
    is nothing on disk -- and getting that backwards flags util/atomic.py, the
    reference implementation this guard tells people to adopt.
  * SOME REACHABLE PATH, not "no unlink anywhere". Crediting any syntactic
    unlink in the collected handlers let `except ValueError: unlink(tmp)` clear a
    rename that raises OSError. Requiring EVERY collected handler to unlink is
    wrong the other way, because handlers nest: an inner one that unlinks and
    re-raises means the outer never sees the temp. Handlers resolve innermost
    first, and a covering `finally` that unlinks dominates all of them.

The "created in this scope" half is load-bearing and was learned expensively. A first version
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
so the ledger is currently honest. But "instance 31 cannot arrive silently" is
true only OUTSIDE the 30 functions already listed, and saying it unqualified was
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
  * A temp named by a MULTI-ARGUMENT join -- `Path(tmpdir, "payload")`. A
    one-argument `Path(x)` is a transparent wrapper and is resolved to `x`; a
    join names a CHILD, and reducing it to its first argument used to record the
    DIRECTORY as the temp, which let a handler removing the directory read as
    cleaning up a child it never touched. Joins are now left intact, so such a
    site is UNMATCHED rather than wrongly cleared -- the safe direction, and a
    real blind spot rather than a fix.
  * A MODULE ALIAS. `import os as _os` then `_os.link(tmp, dst)` produces no
    row: the alias never reaches `owner == "os"`, so the link/move scoping
    excludes it, and for replace/rename the two-argument arity test drops it
    before operand resolution. MEASURED: zero `import os as` / `import shutil as`
    in the scanned tree today, so the published count is honest -- but this is a
    hole, not a design choice.
  * OTHER PUBLISH SHAPES: `Path.hardlink_to`, `shutil.copy2(tmp, final)`,
    `os.symlink`. These are UNHANDLED, not out of class. `final.hardlink_to(tmp)`
    is publish-by-hardlink with the temp as the ARGUMENT, so covering it needs a
    mirrored operand rule rather than the same one -- which is why it is excluded
    rather than added, and excluding it is a limitation to record, not a verdict
    about the shape. MEASURED across ten such verbs: zero live instances where
    the published operand is a born-here temp, so the count stands.
  * A CLEANUP IN ANOTHER SCOPE. `_born_in` stopped crossing scope boundaries
    (a binding in a nested def is a different variable) but `_unlinks` still
    walks freely, so an unlink inside a nested `def _rollback():` that nothing
    calls credits CLEANS_UP. Pre-existing, and the asymmetry is stated here
    rather than left for a reader to find.
  * A `nonlocal` temp created in a nested def and moved in the outer one. The
    own-scope rule drops it -- a recall loss taken deliberately, because the
    alternative reintroduces the cross-scope false positives it exists to stop.
  * A conditional finalizer. `finally: if enabled: tmp.unlink()` reads
    CLEANS_UP. Left alone on purpose: the overwhelmingly common form is
    `if tmp.exists()`, where crediting it is correct.
  * PYTHON UNDER A DOT-DIRECTORY, e.g. `.github/scripts/`. The scanned roots and
    the test that pins them both skip dot-directories. Zero such files today, so
    this is latent rather than live. `Path.rglob` also does not follow symlinked
    directories, so a symlinked subtree inside a scanned root is invisible and
    the completeness test cannot see it either.
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
  4. Anchored on the verb SET, which held only replace/rename/move. Publishing
     by hardlink was therefore invisible -- and so was any temp whose marker sits
     in a module constant rather than inline. Both were found by an adversarial
     audit, not by this guard, on live code added while the guard's own PR was
     open. The lesson is the one already stated above and re-learned anyway: an
     enumeration is only ever a census of the shapes the detector can see, so
     the count is the claim most likely to be wrong.
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
import re
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

#: Scratch stems matched where an f-string's interpolation follows the marker.
#:
#: A SEPARATOR is required before the stem, so `.partition` and `foo.parts` cannot
#: match. That alone was not enough: a first version reused the full suffix list
#: and claimed `f"whats-new-{name}"` as a temp, because the ambiguity lives in the
#: STEM, not the separator. `new` and `part` are ordinary English words that
#: appear mid-name; `tmp`, `temp`, `partial` and `swp` are not. `writing` was in
#: this list and came out: it is plainly ordinary English in a repo that generates
#: content, and an AST sweep of every f-string piece under src/ and scripts/ found
#: zero live sites relying on it, so it was pure risk. `.new`, `.part` and
#: `.writing` all keep working in the strict end-anchored test above, where a
#: TRAILING marker really is a suffix rather than a word in a sentence.
_INTERPOLATED_TEMP_RE = re.compile(r"[._-](tmp|temp|partial|swp)[._-]*$")


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: Calls whose ARGUMENTS can legitimately carry the scratch marker for the path
#: being assigned. Everything else is opaque: a constant buried in an unrelated
#: call's arguments describes that call, not the assigned path.
_PATH_BUILDERS = frozenset(
    {"with_suffix", "with_name", "with_stem", "joinpath", "join", "format", "fspath"}
)

#: ONE-ARGUMENT wrappers that name the same path -- the same set `_TRANSPARENT`
#: already treats as pass-through on the operand side. Omitting them from the
#: builder set did not merely narrow the marker test, it made every
#: `tmp = Path(str(p) + ".tmp")` and even `tmp = Path(mkstemp()[1])` produce NO
#: ROW AT ALL: invisible, and absent from the ledger, which is this guard's own
#: worst outcome. Transparency is gated on ARITY, not on the name, so
#: `Path(tmpdir, "child")` stays opaque -- that is the documented multi-argument
#: join blind spot, and enforcing it by arity keeps the single-argument form.
_PATH_WRAPPERS = frozenset({"Path", "PurePath", "str"})


def _own_scope(node: ast.AST):
    """Walk ``node`` WITHOUT descending into a nested function or class scope.

    `ast.walk` crosses scope boundaries, so a nested `def` that happens to bind
    `tmp` made the OUTER function's durable `tmp` look born-here. A binding in
    another scope is a different variable; treating it as the same one is how a
    durable operand acquires this guard's "unlink the temp" remediation.
    """
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        for child in ast.iter_child_nodes(cur):
            # Prune the CHILD when it opens a new scope; the root is always
            # descended into, which is what makes this work when `node` is
            # itself the FunctionDef being analysed.
            if isinstance(child, _NESTED_SCOPES):
                continue
            stack.append(child)


def _born_in(func: ast.AST, temp_expr: str,
             module_consts: dict[str, str] | None = None,
             before_lineno: int | None = None) -> bool:
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

    ``module_consts`` maps MODULE-LEVEL string constants to their values, so a
    marker held in a constant rather than spelled inline still reads as a temp.
    Without it `f"{stem}{_STAGING_SUFFIX}"` has no ``ast.Constant`` piece at all
    and no amount of relaxing the suffix anchor can reach it -- see the class
    note on _makes_temp.
    """
    if not temp_expr:
        return False
    root = temp_expr.split(".")[0].split("[")[0].strip()
    # Whole-path match FIRST. The root fallback below still has to exist -- a
    # NamedTemporaryFile temp is renamed as `tmp.name`, whose root `tmp` is what
    # the `with` binding recorded -- but it must never be the ONLY test, or an
    # attribute temp anywhere on an object marks every sibling attribute as a
    # temp too.

    def _bound_names(target) -> list[str]:
        """The names a target BINDS -- as whole paths, not every Name beneath it.

        Walking to every `ast.Name` was wrong in the dangerous direction. For
        `self.staging = path.with_suffix(".tmp")` it recorded **`self`** as a
        temp, so a later durable `self.live_file.replace(dst)` matched on the
        shared root and was reported as a leak WITH the unlink remediation --
        the exact durable-operand trap the born-here rule exists to prevent,
        re-entered through the binding side. An attribute or subscript target
        binds ONE path; only a tuple/list target binds several.
        """
        if isinstance(target, (ast.Tuple, ast.List)):
            out: list[str] = []
            for el in target.elts:
                out.extend(_bound_names(el))
            return out
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            return [_unparse(target)]
        if isinstance(target, ast.Starred):
            return _bound_names(target.value)
        return []

    def _reaches(n: ast.AST) -> bool:
        """Does this binding actually precede the move it would explain?

        A whole-function walk marked a name born-here from an assignment LATER
        in the body, so `os.replace(tmp, dst)` followed by
        `tmp = dst.with_suffix('.tmp')` reported the DURABLE first operand as an
        unguarded write -- with the unlink remediation pointed at live data.
        Textual order is an approximation of reaching-definitions (a loop can
        execute a later line first), and it is the SAFE approximation: it can
        only DROP a row, never invent one, and this guard's own history says a
        false row in a debt ledger is worse than a missed one.
        """
        if before_lineno is None:
            return True
        return getattr(n, "lineno", 0) <= before_lineno

    temps: set[str] = set()
    # Seed: `with tempfile.NamedTemporaryFile(...) as tmp:` and friends.
    for n in _own_scope(func):
        if (
            isinstance(n, ast.withitem)
            and n.optional_vars is not None
            and _reaches(n.context_expr)
            and any(m in _unparse(n.context_expr) for m in _TEMP_MAKERS)
        ):
            temps.update(_bound_names(n.optional_vars))

    # Assignments, iterated to a fixpoint so a derived name is reached.
    pairs: list[tuple[list[str], ast.AST]] = []
    for n in _own_scope(func):
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value:
            targets, value = [n.target], n.value
        else:
            continue
        if not _reaches(n):
            continue
        names = [nm for t in targets for nm in _bound_names(t)]
        if names:
            pairs.append((names, value))

    def _makes_temp(value: ast.AST) -> bool:
        """A temp-maker call, or a STRING LITERAL carrying a scratch suffix.

        Anchored to the literal's END rather than matched anywhere in the
        unparsed text: `path.with_suffix(".tmp")` makes a temp, while a variable
        merely named `tmp_dir_listing` does not, and a substring test cannot tell
        them apart.

        F-STRINGS RELAX THAT ANCHOR, and only f-strings. A unique component is
        routinely appended AFTER the marker -- `f".{name}.restore-tmp-{getpid()}"`
        at guardian/cred_integrity.py -- so the marker is mid-literal and the
        end-anchored test produced NO ROW AT ALL for the site, which is worse than
        a wrong verdict because it also left the published site count wrong.
        Inside an f-string a following interpolation EXPLAINS a trailing
        separator, so `-tmp-` there is a scratch marker; in a plain literal it is
        not, and `whats-new-` must keep reading as ordinary text. Hence the
        relaxation is scoped to JoinedStr pieces rather than applied globally.
        """
        def _path_expr(root: ast.AST):
            """Descend, but NOT into the arguments of a call that does not build
            a path. Walking every descendant made
            `record = load_record(excluded_suffix=".tmp")` mark `record` itself
            a temp, so a later one-argument `record.replace(other)` was reported
            as an unguarded write and carried the unlink remediation to a
            non-path object. A marker has to describe the path being ASSIGNED.
            """
            stack = [root]
            while stack:
                cur = stack.pop()
                yield cur
                if isinstance(cur, ast.Call):
                    fn = getattr(cur.func, "attr", None) or getattr(
                        cur.func, "id", None
                    )
                    stack.append(cur.func)  # the receiver chain is still ours
                    transparent = (
                        fn in _PATH_WRAPPERS
                        and len(cur.args) == 1
                        and not cur.keywords
                    )
                    if fn in _PATH_BUILDERS or fn in _TEMP_MAKERS or transparent:
                        stack.extend(cur.args)
                        stack.extend(k.value for k in cur.keywords)
                    continue
                stack.extend(ast.iter_child_nodes(cur))

        for n in _path_expr(value):
            if isinstance(n, ast.Call):
                fn = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if fn in _TEMP_MAKERS:
                    return True
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value.endswith(_TEMP_SUFFIXES)):
                return True
            if isinstance(n, ast.JoinedStr):
                for piece in n.values:
                    # A marker held in a module-level constant: the f-string has
                    # NO Constant piece, so the suffix tests below can never see
                    # it. End-anchored here, not _INTERPOLATED_TEMP_RE -- the
                    # constant's own text is the whole marker, with no following
                    # interpolation inside it to explain a trailing separator.
                    if (isinstance(piece, ast.FormattedValue)
                            and isinstance(piece.value, ast.Name)
                            and not _locally_rebound(func, piece.value.id)
                            and (module_consts or {}).get(
                                piece.value.id, "").endswith(_TEMP_SUFFIXES)):
                        return True
                    if not (isinstance(piece, ast.Constant)
                            and isinstance(piece.value, str)):
                        continue
                    if _INTERPOLATED_TEMP_RE.search(piece.value):
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
            # Attribute paths are compared WHOLE for the same reason `_bound_names`
            # binds them whole -- `self.staging` is a temp, `self` is not.
            refs = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
            refs |= {
                _unparse(n) for n in ast.walk(value) if isinstance(n, ast.Attribute)
            }
            if (refs & temps) and not set(names) <= temps:
                temps.update(names)
                grew = True
        if not grew:
            break

    # Whole path, then any PROPER PREFIX of it, then the bare root.
    #
    # The prefix rung is what makes the attribute case symmetric with the name
    # case. Binding whole paths fixed a false FLAG (`self` is not a temp), but on
    # its own it created a false CLEAN, which is strictly worse: for
    # `self.handle = NamedTemporaryFile(...)` renamed as `self.handle.name`,
    # neither the full path (`self.handle.name`) nor the bare root (`self`, no
    # longer bound) matched, so the site produced NO ROW -- invisible AND absent
    # from the debt ledger. MEASURED: that shape read LEAKS before the binding
    # change and [] after it.
    #
    # Prefixes stop SHORT of the bare root on purpose. Including it would restore
    # exactly the sibling bug this fix removed, since every `self.x` shares the
    # root `self`. The root disjunct stays for the case it was written for -- a
    # temp bound as a bare NAME and renamed as `tmp.name`.
    parts = temp_expr.split(".")
    prefixes = {".".join(parts[:i]) for i in range(2, len(parts))}
    return temp_expr in temps or root in temps or bool(prefixes & temps)


_TRY_NODES: tuple = (
    (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)
)


def _handlers_covering(func: ast.AST, lineno: int) -> list[tuple[list, list | None]]:
    """Handlers and finalizers that actually run if the move at ``lineno`` raises.

    The two suites have DIFFERENT reach, and collapsing them was the bug:

    * ``except`` handlers catch only what the try BODY raises. A rename sitting
      inside an ``except:`` block is not protected by that same handler, which is
      why this tests ``n.body`` rather than the whole Try node.
    * ``finally`` runs if the move raises ANYWHERE in the statement -- body,
      an ``except`` suite, or the ``else`` suite. Testing only ``n.body`` meant a
      move in an ``else:`` with ``finally: tmp.unlink(missing_ok=True)`` was
      reported NO_HANDLER while the cleanup demonstrably runs.

    ``ast.TryStar`` (except*) is the same statement for this purpose and was
    invisible to an isinstance test naming only ``ast.Try``.
    """

    def _spans(suite: list) -> bool:
        return any(c.lineno <= lineno <= (c.end_lineno or c.lineno) for c in suite)

    found: list = []
    for n in ast.walk(func):
        if not isinstance(n, _TRY_NODES):
            continue
        in_body = _spans(n.body)
        excepts = list(n.handlers) if in_body else []
        # The finalizer covers the body, the handlers and the else suite alike.
        final = (
            n.finalbody
            if n.finalbody
            and (in_body or _spans(n.orelse) or any(_spans(h.body) for h in n.handlers))
            else None
        )
        if excepts or final:
            span = (n.end_lineno or n.lineno) - n.lineno
            found.append((span, excepts, final))
    # INNERMOST FIRST. Nesting is not a flat pool of handlers: an inner
    # `except BaseException:` that unlinks and re-raises means the OUTER handler
    # never sees the move exception with the temp still on disk. Treating every
    # enclosing try as an equally applicable path reported four genuinely clean
    # live sites as LEAKS -- exactly the false-flag noise that makes a debt
    # ledger stop being read.
    found.sort(key=lambda t: t[0])
    return [(e, f) for _, e, f in found]


#: Calls that put BYTES IN the temp. Creation (`mkstemp`, `NamedTemporaryFile`)
#: is deliberately absent: if creation itself fails there is nothing on disk to
#: leak, so an uncovered creation is not the risk window -- an uncovered WRITE
#: is. Getting that distinction wrong flags util/atomic.py, the repo's own
#: reference implementation, whose mkstemp sits outside the try while the write
#: it protects sits inside.
_WRITE_CALLS = frozenset(
    {"write_text", "write_bytes", "write", "writelines", "writerow", "writerows",
     "fdopen", "open", "copy", "copy2", "copyfile", "copyfileobj", "dump",
     "safe_dump"}
)


#: A filesystem move raises OSError. A handler naming a class that cannot catch
#: one does not protect the move at all; a handler naming a SUBCLASS protects
#: only part of it. Both distinctions were missing, so `except ValueError:
#: unlink(tmp)` credited cleanup for a rename that raises OSError -- a FALSE
#: CLEAN, which hides the leak AND keeps it out of the ledger.
_FULLY_CATCHES = frozenset(
    {"OSError", "IOError", "EnvironmentError", "Exception", "BaseException"}
)


def _handler_classes(handler: ast.ExceptHandler) -> list[str]:
    """The exception names a handler catches; empty list means a bare except."""
    t = handler.type
    if t is None:
        return []
    parts = t.elts if isinstance(t, ast.Tuple) else [t]
    return [_unparse(x).rsplit(".", 1)[-1] for x in parts]


def _fully_catches_move(handler: ast.ExceptHandler) -> bool:
    """Does this handler catch EVERY error a move can raise?"""
    names = _handler_classes(handler)
    return not names or any(n in _FULLY_CATCHES for n in names)


#: Every class a move can actually raise, plus the superclasses that catch them.
#: ALLOWLIST, NOT DENYLIST -- the house rule, and it is load-bearing here. The
#: first version asked "does this name end in Error and miss a 12-entry denylist"
#: and called anything matching APPLICABLE. Combined with the all-applicable rule
#: below, a sibling `except RuntimeError: raise` next to an `except OSError:` that
#: unlinks correctly turned the whole site LEAKS. MEASURED on this tree: 296 try
#: statements carry two or more handlers, and 590 handler occurrences name an
#: *Error the denylist admitted that cannot catch an os.replace -- RuntimeError
#: 35, CancelledError 32, YAMLError 14, OperationalError 14, SubprocessError 14.
#: The generosity argument holds for ONE handler and does not survive `all(...)`.
#: An absent name is safe in both directions: it makes the handler non-applicable,
#: which continues the outward walk rather than crediting cleanup.
_CATCHES_MOVE = _FULLY_CATCHES | {
    "FileNotFoundError", "FileExistsError", "PermissionError", "IsADirectoryError",
    "NotADirectoryError", "TimeoutError", "InterruptedError", "BlockingIOError",
    "ProcessLookupError", "ChildProcessError", "ConnectionError", "BrokenPipeError",
    "SameFileError", "SpecialFileError",
}


def _unlinks_on_success(scope: ast.AST, temp: str, lineno: int, covering: list) -> bool:
    """Is the temp removed on the path where the publish SUCCEEDS?

    Two ways to qualify:
      * a covering `finally` that unlinks -- it runs on every path out, success
        included;
      * an unlink AFTER the publish that is not inside an `except` handler, i.e.
        one the normal flow reaches.

    An unlink reachable only from a handler does not qualify: that path is the
    failure path, and a hardlink publish leaks on the SUCCESS path.
    """
    if any(f is not None and _unlinks([f], temp, scope) for _, f in covering):
        return True
    handler_bodies = [
        h.body for n in ast.walk(scope) if isinstance(n, _TRY_NODES)
        for h in n.handlers
    ]

    def _in_handler(line: int) -> bool:
        return any(
            any(c.lineno <= line <= (c.end_lineno or c.lineno) for c in body)
            for body in handler_bodies
        )

    for n in ast.walk(scope):
        if not isinstance(n, ast.Call) or n.lineno <= lineno:
            continue
        if _in_handler(n.lineno):
            continue
        if _unlinks([n], temp, scope):
            return True
    return False


def _reraises(handler: ast.ExceptHandler) -> bool:
    """Does this handler end by re-raising, so an OUTER handler still runs?

    Only a BARE `raise` counts. `raise SomethingElse` propagates a different
    class, which an enclosing `except OSError` would not catch, so treating it
    as pass-through would credit cleanup that never happens.
    """
    return any(
        isinstance(n, ast.Raise) and n.exc is None
        for n in _own_scope(ast.Module(body=handler.body, type_ignores=[]))
    )


def _can_catch_move(handler: ast.ExceptHandler) -> bool:
    """Could this handler catch SOMETHING a move raises? Subclasses count."""
    names = _handler_classes(handler)
    return not names or any(n in _CATCHES_MOVE for n in names)


#: Wrappers that do not change WHICH path is meant, so `Path(tmp)`, `str(tmp)`
#: and `tmp.expanduser()` all still name `tmp`.
_TRANSPARENT = ("Path", "str", "os.fspath", "pathlib.Path")


def _strip_wrappers(expr: ast.AST) -> ast.AST:
    """Remove path-neutral wrappers: Path(x), str(x), x.expanduser()/resolve().

    A wrapper is only transparent when it takes ONE argument. `Path(x)` names the
    same path as `x`; `Path(tmpdir, "payload")` names a CHILD of `tmpdir`, and
    reducing it to its first argument recorded the DIRECTORY as the temp. That
    matters in the dangerous direction: a handler unlinking `tmpdir` was then
    credited with cleaning up a child it never removed, so a leak read CLEANS_UP.
    Multi-argument joins are left intact, which at worst makes the site unmatched
    rather than wrongly cleared.
    """
    node = expr
    for _ in range(8):  # bounded; nesting deeper than this is not real code
        if (isinstance(node, ast.Call) and _unparse(node.func) in _TRANSPARENT
                and len(node.args) == 1 and not node.keywords):
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
                # `remove` DELETES A FILE only on os/shutil. A list's
                # `.remove(x)` drops an ELEMENT, and crediting it as cleanup made
                # a genuinely leaking site read CLEANS_UP -- the false-clean
                # direction this function's docstring calls strictly worse, since
                # the leak is then invisible AND excluded from the ledger.
                # `unlink` needs no such test: no builtin container has one.
                # A bare `from os import remove` is deliberately NOT credited --
                # that costs a false FLAG, which is the direction this function
                # is allowed to be wrong in.
                if fn == "remove" and not (
                    isinstance(n.func, ast.Attribute)
                    and _unparse(n.func.value) in ("os", "shutil")
                ):
                    continue
                targets: set[str] = set()
                for a in n.args:
                    targets.add(_core_name(a))
                    targets.add(_resolve(a, aliases))
                # KEYWORD form too: `os.unlink(path=tmp)` cleans up exactly as
                # the positional spelling does, and reading only n.args reported
                # the protected site as LEAKS.
                for kw in n.keywords:
                    if kw.arg in (None, "path"):
                        targets.add(_core_name(kw.value))
                        targets.add(_resolve(kw.value, aliases))
                if isinstance(n.func, ast.Attribute):
                    targets.add(_core_name(n.func.value))
                    targets.add(_resolve(n.func.value, aliases))
                if wants & targets:
                    return True
    return False


def _operand(call: ast.Call, index: int, *names: str) -> ast.AST | None:
    """The operand at `index`, or under any of `names` if passed by keyword.

    Indexing `call.args` alone dropped the entire keyword form: `os.replace(
    src=tmp, dst=target)` has NO positional arguments, so an arity test read it
    as "not a filesystem move" and the site produced no row. Silent, and
    invisible in every count.
    """
    if len(call.args) > index:
        return call.args[index]
    for kw in call.keywords:
        if kw.arg in names:
            return kw.value
    return None


def _locally_rebound(func: ast.AST | None, name: str) -> bool:
    """Does this function bind `name` itself, shadowing the module-level import?

    Matching an imported name with no scope analysis let a local
    `replace = mapping["fn"]`, a parameter called `move`, and a nested
    `def replace` all produce rows. Latent -- no file in this tree imports these
    names directly -- but a false LEAK row is a booby-trapped work item whose
    printed remediation says to unlink a durable file, so this is cheap
    insurance rather than a response to a live sighting.
    """
    if func is None:
        return False
    for n in ast.walk(func):
        if isinstance(n, ast.arg) and n.arg == name:
            return True
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return True
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in n.targets
        ):
            return True
        if (
            isinstance(n, (ast.AnnAssign, ast.NamedExpr))
            and isinstance(n.target, ast.Name)
            and n.target.id == name
        ):
            return True
    return False


def _module_str_consts(tree: ast.AST) -> dict[str, str]:
    """MODULE-LEVEL ``NAME = "literal"`` bindings, for temp-marker resolution.

    Module level is the SOURCE, and that alone is not the safety property.
    Collecting only module-level names does NOT stop a name being misresolved
    inside a function that shadows it -- a parameter, a local assign, an
    AnnAssign, a walrus or a nested def all rebind it, and the symbol table
    still answers with the module's value. The property lives at the USE site:
    `_born_in` gates this table behind `_locally_rebound`, exactly as the
    `_directly_imported_moves` call site does. That pairing is the invariant;
    neither half is sufficient alone.

    MEASURED: without the use-site gate, a module-level `SUFFIX = ".tmp"` plus
    a local `SUFFIX = ".jsonl"` produced a LEAKS row on a DURABLE file, and so
    did the same name arriving as a parameter -- a row whose printed remediation
    is "unlink the temp".
    """
    out: dict[str, str] = {}
    for n in getattr(tree, "body", []):
        if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Constant):
            continue
        if not isinstance(n.value.value, str):
            continue
        for t in n.targets:
            if isinstance(t, ast.Name):
                out[t.id] = n.value.value
    return out


def _directly_imported_moves(tree: ast.AST) -> dict[str, str]:
    """Names bound by `from os import replace` and friends, mapped to a module.

    The guard required an ATTRIBUTE call, which is what excludes
    `dataclasses.replace`'s bare form -- but it also excluded a genuine
    `from os import replace; replace(tmp, target)`. The fix is not to drop the
    attribute rule (that class of false positive is the largest this guard has
    had) but to allowlist the names an `os`/`shutil` import actually binds.

    The value is the MODULE, so `from shutil import move` keeps shutil semantics
    downstream instead of being flattened to "os" -- correct today only because
    one later condition happens to be spelled defensively, which is not a thing
    to rely on. Only MODULE-LEVEL imports count: one inside a function or a
    `try:` fallback binds a name whose scope this guard does not model, and
    guessing there is exactly how a false row gets made.
    """
    out: dict[str, str] = {}
    for n in getattr(tree, "body", []):
        if isinstance(n, ast.ImportFrom) and n.module in ("os", "shutil"):
            for alias in n.names:
                if alias.name in ("replace", "rename", "move", "link"):
                    out[alias.asname or alias.name] = n.module
    return out


def analyse_source(src: str, rel: str) -> list[dict]:
    """Every atomic-write site in one file, classified. Pure; no I/O."""
    tree = ast.parse(src)
    imported_moves = _directly_imported_moves(tree)
    module_consts = _module_str_consts(tree)
    rows: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # A filesystem move is ALWAYS an attribute call. A bare `replace(x, ...)`
        # is dataclasses.replace -- the largest false-positive class this guard
        # had, and nothing about it touches a filesystem.
        if isinstance(node.func, ast.Name):
            # Bare call: ONLY the names an os/shutil import actually bound. A
            # bare `replace(rec, f=1)` is dataclasses and must stay excluded.
            if node.func.id not in imported_moves:
                continue
            if _locally_rebound(_enclosing_func(tree, node.lineno), node.func.id):
                continue
            owner, receiver = imported_moves[node.func.id], None
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr not in ("replace", "rename", "move", "link"):
                continue
            owner, receiver = _unparse(node.func.value), node.func.value
        else:
            continue
        # `shutil.move(src, dst)` is the same operation and was a straight blind
        # spot: MEASURED 5 live call sites, one of them three lines above a
        # baselined leak in the same function.
        verb = getattr(node.func, "attr", None) or getattr(node.func, "id", "")
        if verb == "move" and owner != "shutil" and receiver is not None:
            continue
        # PUBLISH-BY-HARDLINK. `os.link(staging, final)` is the same class: a
        # temp made here becomes the durable record under another name. It is
        # MORE squarely in the class than rename, not less -- link does not
        # consume its source, so the temp ALWAYS needs an explicit unlink.
        # Scoped to `os` exactly as `move` is scoped to `shutil`: an arbitrary
        # `obj.link(x)` would otherwise be read as receiver-is-the-temp, and
        # `Path.hardlink_to` reverses the operands outright.
        if verb == "link" and owner != "os":
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
        # os.replace(src, dst) two but is named unambiguously. COUNT THE KEYWORD
        # too: `Path(tmp).replace(target=dest)` has zero positional args, so a
        # purely positional test dropped it HERE, before the resolution below
        # ever ran -- which made that branch's "target" name dead code and made
        # _operand's "before any arity test" promise false for it.
        if owner not in ("os", "shutil") and (
            len(node.args) + sum(1 for k in node.keywords if k.arg == "target")
        ) != 1:
            continue
        # Resolve both operands by POSITION OR KEYWORD: the kwarg-only spelling
        # has no positional args and used to fall through to `temp = "os"`,
        # crediting any os.unlink in a handler as cleanup.
        if owner in ("os", "shutil"):
            src_expr = _operand(node, 0, "src")
            dst_expr = _operand(node, 1, "dst")
        else:
            src_expr, dst_expr = receiver, _operand(node, 0, "target")
        if src_expr is None or dst_expr is None:
            continue
        # WHICH OPERAND IS THE TEMP depends on the form. Getting this wrong
        # silently checks the DESTINATION for cleanup instead of the temp.
        # The receiver keeps its wrappers, and `Path(tmp).replace(dest)` is a
        # common house style here -- MEASURED invisible at ego/config.py:98,
        # mcp/health/settings.py:706 and outreach/config.py:231, which produced
        # NO ROW at all. `_born_in` looks up a bare NAME, and `Path(tmp_path)` is
        # not one, so the site was silently dropped rather than judged. Strip
        # first, then take the temp.
        temp = _unparse(_strip_wrappers(src_expr))
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
        # MODULE SCOPE IS A SCOPE. This used to skip the born-here test whenever
        # `func` was None and then stamp NO_HANDLER unconditionally, which was
        # wrong in BOTH directions at once: a module-level move-aside like
        # `os.replace(target, aside)` produced a dirty row for a DURABLE first
        # operand -- printing "unlink the temp" at live data, the booby-trap this
        # guard exists not to make -- while a module-level atomic write that DOES
        # unlink in except/finally still read NO_HANDLER. Both `_born_in` and
        # `_handlers_covering` take any AST node, so the module tree is simply
        # the enclosing scope. Found independently by two reviewers, which is
        # what moved it from "known limitation" to defect.
        scope = func if func is not None else tree
        if not _born_in(scope, temp, module_consts, before_lineno=node.lineno):
            continue
        # EVERY APPLICABLE PATH, not ANY handler node -- but resolved by NESTING,
        # innermost first. Crediting a site because one syntactic unlink appeared
        # anywhere in the collected handlers let `except ValueError: unlink(tmp)`
        # clear a rename that raises OSError, and let a multi-handler try pass
        # when only ONE handler unlinked. Flattening every enclosing try instead
        # is wrong the other way: an inner handler that unlinks and re-raises
        # means the outer one never sees the temp. So walk outward and stop at
        # the first try that actually decides the move's fate.
        covering = _handlers_covering(scope, node.lineno)
        # A COVERING `finally` THAT UNLINKS DOMINATES EVERYTHING BENEATH IT, so
        # it is tested across all enclosing levels BEFORE any handler reasoning.
        # Walking innermost-first and concluding from handlers alone reported
        # inbox/writer.py's `_allocate_and_link` as a leak: its inner
        # `except FileExistsError: continue` does not unlink, but the outer
        # try/finally unlinks on every path out, so the temp never survives.
        verdict = "NO_HANDLER"
        saw_applicable = False
        if any(f is not None and _unlinks([f], temp, scope) for _, f in covering):
            verdict = "CLEANS_UP"
            covering = []
        for excepts, _final in covering:
            applicable = [h for h in excepts if _can_catch_move(h)]
            saw_applicable = saw_applicable or bool(applicable)
            if not applicable:
                # A finalizer that does not unlink DECIDES NOTHING -- an
                # enclosing handler can still clean up, and the dominance check
                # above already proved no covering finalizer unlinks. Concluding
                # LEAKS here abandoned the outward walk and flagged the idiomatic
                # `try: write; move; finally: lock.release()` wrapped in an outer
                # `except OSError: tmp.unlink()` as a leak.
                continue                    # this try cannot decide the move

            # A handler that RE-RAISES has not finished the job: the exception
            # keeps propagating and an enclosing handler still runs. So
            # `except OSError: log.warning(...); raise` decides nothing and the
            # walk continues outward, rather than reading as a leak because this
            # particular handler did not unlink. A handler that unlinks AND
            # re-raises HAS finished the job and still decides -- reading the
            # bare `raise` alone excluded session_cache.py's
            # `except BaseException: unlink(tmp); raise`, whose outer handler
            # does not unlink, and flagged a correct live site.
            deciding = [
                h for h in applicable
                if _unlinks([h.body], temp, scope) or not _reraises(h)
            ]
            if not deciding:
                continue
            cleaned = all(_unlinks([h.body], temp, scope) for h in deciding)
            if not cleaned:
                verdict = "LEAKS"           # some catchable path leaves the temp
                break
            if any(_fully_catches_move(h) for h in deciding):
                verdict = "CLEANS_UP"       # nothing escapes this try uncleaned
                break
            # Partial cover that DOES unlink: keep looking outward for the rest.
            verdict = "LEAKS"

        # A walk that ran out of enclosing trys without deciding still SAW a
        # handler. NO_HANDLER would be the wrong label -- both are dirty, so the
        # guard catches it either way, but the ledger and its reader deserve the
        # accurate one. This is the `except OSError: unlink(other); raise` shape:
        # a handler exists, it just never cleans up this temp.
        if verdict == "NO_HANDLER" and saw_applicable:
            verdict = "LEAKS"

        # THE WHOLE SEQUENCE, not just the rename. A handler wrapped around only
        # the move leaves the temp on disk when the WRITE fails -- disk-full, an
        # encoding error, a short write -- which is the very class this guard is
        # named for. MEASURED 2026-09-09: no live site is reclassified by this,
        # so it is coverage rather than churn.
        if verdict == "CLEANS_UP":
            # IDENTITY, not containment, and EVERY write, not the earliest.
            # `root in _unparse(n)` was the substring defect `_unlinks` already
            # documents, re-introduced on the write side: an uncovered
            # `tmp_sidecar.write_text(...)` downgraded a clean `tmp` site. And
            # checking only min(writes) let a covered first write followed by an
            # UNCOVERED append read CLEANS_UP -- the false-clean direction.
            #
            # STATED LIMITATION: the coverage test here is existence of a
            # covering try, not the full applicability/unlink reasoning above.
            # A write wrapped in `except ValueError: pass` therefore counts as
            # covered. Closing that means factoring the verdict loop into a
            # helper, which is a larger change than this belongs in.
            want_node = ast.parse(temp, mode="eval").body
            aliases = _alias_map(scope)
            wants = {_core_name(want_node), _resolve(want_node, aliases)}
            for n in ast.walk(scope):
                if not isinstance(n, ast.Call) or n.lineno > node.lineno:
                    continue
                fn = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if fn not in _WRITE_CALLS:
                    continue
                operands: set[str] = set()
                for a in list(n.args) + [k.value for k in n.keywords]:
                    operands.add(_core_name(a))
                    operands.add(_resolve(a, aliases))
                if isinstance(n.func, ast.Attribute):
                    operands.add(_core_name(n.func.value))
                    operands.add(_resolve(n.func.value, aliases))
                if (wants & operands) and not _handlers_covering(scope, n.lineno):
                    verdict = "LEAKS"
                    break
        # PUBLISH-BY-HARDLINK NEEDS CLEANUP ON THE **SUCCESS** PATH TOO.
        # `os.link` does not consume its source: after a SUCCESSFUL publish the
        # staging entry is still on disk, so exception-only cleanup leaks once
        # per successful write -- the common case, not the error case. This file
        # already said "link does not consume its source, so the temp ALWAYS
        # needs an explicit unlink" when the verb was added, and then routed link
        # sites through the rename verdict anyway, which contradicted it. A
        # covering `finally` satisfies this (it runs on success); so does an
        # unlink on the normal path after the link. An unlink reachable only from
        # an `except` does not.
        if verdict == "CLEANS_UP" and verb == "link" and not _unlinks_on_success(
            scope, temp, node.lineno, covering
        ):
            verdict = "LEAKS"
        rows.append({"file": rel, "line": node.lineno,
                     # `_qualname` dereferences node.name, so module scope keeps
                     # its literal label rather than being passed None. The old
                     # early-return supplied this; folding module scope into the
                     # normal path removed that and left an AttributeError that
                     # no LIVE file triggers -- there is no module-level atomic
                     # write in the tree today, so the guard stayed green while
                     # carrying a crash for the first one anybody adds.
                     "func": _qualname(tree, func) if func is not None
                     else "<module>",
                     "temp": temp, "verdict": verdict})
    return rows


def key(row: dict) -> str:
    """Baseline identity. Deliberately EXCLUDES the line number, which shifts on
    every unrelated edit above it and would turn the ledger into churn."""
    return f"{row['file']}::{row['func']}::{row['temp']}"


#: Top-level trees that SHIP EXECUTABLE PYTHON and are therefore scanned.
#: `az_plugins/` was missed for exactly the reason a hardcoded list gets things
#: wrong -- it did not exist when the list was written. MEASURED 2026-09-09: it
#: holds 11 modules and produces ZERO rows, so adding it moves no count; the gap
#: was in COVERAGE, not in the verdicts. `_UNSCANNED_ROOTS` records the
#: deliberate exclusions, and a test pins that every top-level tree with Python
#: in it appears in one list or the other -- so the next tree cannot be missed
#: silently the way this one was.
SCAN_ROOTS = ("src", "scripts", "az_plugins")

#: Excluded ON PURPOSE, with the reason, because absence teaches nothing.
UNSCANNED_ROOTS = {
    "tests": "fixtures create and abandon temp files BY DESIGN; every row would "
             "be noise, and a debt ledger full of noise stops being read",
}


def scan(repo: Path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for base in SCAN_ROOTS:
        if not (repo / base).is_dir():
            continue
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
