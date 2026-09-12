#!/usr/bin/env python3
"""Migration-id guard: every file in a migrations directory is a legal migration.

Replaces the shell glob+grep that lived in ci.yml. That version could not
survive the move to timestamp ids in either direction, both measured
2026-09-03 against a mixed directory:

  * its glob ``[0-9][0-9][0-9][0-9]_*.py`` made every timestamp migration
    INVISIBLE to the duplicate check — the guard silently stopped guarding;
  * widening the glob while keeping ``grep -oP '^\\d{4}'`` truncated
    ``20260903200000`` to ``2026``, so any two same-year migrations read as a
    duplicate of each other — CI red on the second one ever written.

Both failures come from re-implementing the runners' filename semantics in a
second language. This script instead IMPORTS the authoritative contract from
``src/genesis/db/_migration_ids.py``, so the guard cannot drift from what
actually runs (the same reason the tree-wide test imports it rather than
copying).

It imports that module and NOT the runners, which was the first attempt: the
runners carry module-scope ``import aiosqlite`` / ``from genesis.db.crud …``,
so binding to them pulled 117 modules into a CI job that installs nothing and
exited 2 on every PR — a guard that enforced neither rule while every test
passed, because pytest runs inside the project venv. ``_migration_ids`` is
stdlib-only precisely so this path import is safe under a bare ``python3``;
``test_ci_command_line_*`` runs this script the way CI does so that premise
stays true.

THIS GUARD IS STRICTER THAN THE RUNTIME, ON PURPOSE. Discovery refuses only
files that CANNOT RUN; a well-formed 4-digit id outside this repo's frozen set
runs there with a warning, because refusing it on the boot-abort path would
leave a downstream fork carrying its own ``0094_*.py`` with no database after a
pull — over a policy of ours, not an invariant of theirs. CI is the layer that
may enforce this repo's namespace, so the freeze is refused HERE.

Rules, all fail-closed:

1. **Every file is classified, and the residue is a violation.** A name that
   presents as a migration but does not parse as one — a mistyped width, a
   ``d`` prefix in the wrong directory, a non-ASCII digit, a legacy-width id
   outside the frozen set — is reported. Its predecessor matched a regex and
   skipped whatever failed, which is the same silence the runtime had: the file
   is discovered by nothing and NEVER RUNS, so the code that needs it deploys
   against a schema that never got the change.
2. **No duplicate id** in either namespace. Both runners raise on one — the
   schema runner aborts bootstrap on every install, the data runner skips its
   whole batch post-boot — so this stays enforced for timestamp ids too (two
   authors can hit the same second, however unlikely).
3. **Every frozen legacy file is still present, under its own name.** The
   frozen set is enumerated, not a range, so this catches a DELETED legacy
   migration and a RENAMED one (``0050_old.py`` -> ``0050_new.py`` leaves the
   id set identical while existing installs skip the file forever and fresh
   installs run it). It is a statement about the default branch, which is why
   it lives here and not in the runtime: CI checks out the merge ref, so main's
   files are present, but an individual install legitimately sits on an older
   commit with fewer of them.
4. **No FUTURE timestamp id.** The ordering invariant's floor is guarded in the
   contract (a year >= the epoch forces a leading '2', so timestamps sort after
   legacy ids); the ceiling is guarded here, where "future" can be measured
   against the present instead of a static cap that would age badly. One wrong
   digit — 2926 for 2026 — is a valid id that sorts after everything anyone
   writes for centuries, and is a likelier typo than the widths already caught.
5. **No migration in a SUBDIRECTORY.** Discovery is flat because importlib
   resolves a flat package, so a nested file is discovered by nothing: the same
   never-runs silence as rule 1, in the one shape a flat scan cannot see.
6. **Nothing already MERGED disappears.** Rule 3 is an enumerated list, and a
   list can only hold a closed set: the legacy ids. Timestamp ids are unbounded,
   so the moment the first one merges, deleting or renaming it passes every rule
   above — existing installs keep its id in their ledger and skip the
   replacement forever. "Already merged" is a fact only git holds, so this rule
   asks git (``--base``) instead of transcribing it into a list that goes stale.
   A rename is a delete plus an add, so no rename detection is needed. An
   explicitly-passed base that cannot be READ is a violation, because a guard
   told to compare that then compares nothing is the silent pass this file
   exists to remove; with no base given at all the rule is skipped, so a fork
   running from a tarball is not failed for lacking history.

Read-only. Exit 0 clean, 1 on any violation, 2 on a usage/plumbing error
(never a silent pass).
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import traceback
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_id_contract(repo_root: Path):
    """Path-import ``db/_migration_ids`` and return it.

    By path, not by package import: CI installs nothing, so
    ``import genesis.db…`` is unavailable. That module is stdlib-only by
    contract (its own docstring says so and this is why), which is what makes
    a bare-interpreter path import safe here. Not registered in ``sys.modules``
    — it defines no dataclasses, so nothing needs the self-reference, and
    leaving the process clean avoids the cross-test contamination a previous
    runner-importing version created.
    """
    path = repo_root / "src/genesis/db/_migration_ids.py"
    spec = importlib.util.spec_from_file_location("_migration_ids", path)
    if spec is None or spec.loader is None:  # pragma: no cover - plumbing
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Where the "already merged" question is answered when nobody says otherwise.
_DEFAULT_BASE_REF = "origin/main"


def _runnable_names_at(repo_root: Path, ref: str, rel_dir: str, ids) -> set[str] | None:
    """Runnable migration filenames present in *rel_dir* at git *ref*.

    ``None`` means the question could not be ASKED — no git, no such ref, a
    tarball checkout. That is not the same answer as "nothing was deleted", and
    the caller keeps the two apart.

    Filtered through the SAME :func:`classify` the on-disk scan uses, so a name
    that never ran (malformed on the base) is not defended: removing it is a
    fix, not a regression. A rename is a delete plus an add, so it needs no
    rename detection to be caught — the old name is simply gone.
    """
    try:
        # -z: NUL-delimited RAW paths. Without it git C-quotes any non-ASCII
        # path ("…caf\303\251.py", quotes included), the quoted form matches no
        # classify() verdict, and the file silently drops out of base_names —
        # so a Unicode-named migration could be deleted or renamed undetected,
        # the exact divergence this rule exists to catch. MEASURED 2026-09-04
        # against a real tree (default output quoted; -z byte-exact).
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", "-z", ref, "--", rel_dir],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    names = {PurePosixPath(line).name for line in out.stdout.split("\0") if line.strip()}
    label = ids.SCHEMA if rel_dir.endswith("/migrations") else ids.DATA
    return {n for n in names if ids.classify(label, n) in ids.RUNNABLE}


def check(repo_root: Path, *, base_ref: str | None = None) -> list[str]:
    """Return a list of human-readable violations (empty == clean).

    ``base_ref`` names the commit that answers "what was already merged". Pass
    it explicitly (CI does) and an unreadable ref becomes a VIOLATION: a guard
    that was configured to compare and then quietly compared nothing is the
    silent-pass this whole file exists to remove. Left unset, the check falls
    back to ``origin/main`` and DEGRADES to a printed note if that is not
    resolvable — a fork running this from a tarball has no base to compare
    against and should not be failed for it.
    """
    ids = _load_id_contract(repo_root)
    surfaces = [
        (ids.SCHEMA, "src/genesis/db/migrations"),
        (ids.DATA, "src/genesis/db/data_migrations"),
    ]
    ref = base_ref or _DEFAULT_BASE_REF

    violations: list[str] = []
    for label, rel_dir in surfaces:
        directory = repo_root / rel_dir
        if not directory.is_dir():
            # Fail CLOSED: the id contract now lives OUTSIDE these directories,
            # so a missing one no longer fails at load — and a scan that never
            # read a directory must never read as clean.
            violations.append(
                f"{label}s: directory not found at {directory} — nothing was scanned"
            )
            continue

        # ONE scan, shared with the runtime, so the guard cannot classify a file
        # differently from the runner that will execute it.
        runnable, unrunnable, disallowed = ids.scan_directory(label, directory)

        # The runtime RUNS a disallowed-but-runnable id and only warns, because
        # refusing it there would brick a downstream fork over a rule of ours.
        # CI is the layer that may enforce this repo's namespace policy.
        for reason in unrunnable + disallowed:
            violations.append(f"{label}s: {reason}")

        by_prefix: dict[str, list[str]] = defaultdict(list)
        seen: set[str] = set()
        for migration_id, _stem, path in runnable:
            seen.add(path.name)
            by_prefix[migration_id].append(path.name)
            # The ordering invariant is guarded at its floor by
            # is_valid_timestamp_id (year >= epoch, so a timestamp sorts after
            # every legacy id). Nothing bounded the other end, and the typo that
            # hits it is far likelier than the ones that were caught: one wrong
            # digit turns 2026 into 2926, which is a perfectly valid id that
            # sorts after everything anyone writes for the next 900 years.
            #
            # The ceiling lives HERE and not in the contract because it is
            # relative to now: a static cap would become a time bomb for an
            # install running years from now, while CI always runs in the
            # present.
            core = migration_id.lstrip("d")
            if len(core) == 14:
                stamp = datetime.strptime(core, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
                if stamp > datetime.now(UTC) + timedelta(days=1):
                    violations.append(
                        f"{label}s: {path.name!r} carries a FUTURE timestamp id "
                        f"{core!r}. It would sort after every migration written "
                        f"between now and then, so it runs last on a fresh "
                        f"install and in sequence on an existing one. "
                        f"Regenerate it with `date -u +%Y%m%d%H%M%S`."
                    )

        # Discovery is FLAT (importlib resolves a flat package), so a migration
        # one directory down is discovered by nothing — the same never-runs
        # silence this guard exists to close, left open for one shape.
        for nested in sorted(directory.rglob("*.py")):
            if nested.parent != directory and ids.CANDIDATE_PATTERN.match(nested.name):
                violations.append(
                    f"{label}s: {nested.relative_to(directory)} sits in a "
                    "subdirectory of the migrations package. Discovery is flat, "
                    "so it would never run. Move it up one level."
                )

        if not by_prefix:
            violations.append(
                f"{label}s: no runnable migration found in {directory} — "
                "not a possible state, so the scan did not describe the directory"
            )
            continue

        for prefix, names in sorted(by_prefix.items()):
            if len(names) > 1:
                violations.append(
                    f"{label}s: duplicate id '{prefix}' — {', '.join(sorted(names))}. "
                    "Rename one to a fresh UTC timestamp id "
                    "(`date -u +%Y%m%d%H%M%S`)."
                )

        # An id-set check cannot see this: deleting or renaming a frozen file
        # leaves min/max/count untouched, while existing installs — which have
        # the id in `schema_migrations` — skip it forever and fresh installs
        # run it. The two schemas then diverge with nothing to notice.
        missing = sorted(ids.FROZEN_LEGACY_FILES[label] - seen)
        if missing:
            violations.append(
                f"{label}s: {len(missing)} frozen legacy file(s) missing from "
                f"{directory}: {', '.join(missing)}. That namespace is frozen — "
                "an applied migration may not be deleted or renamed, because "
                "installs that already ran it will never run the replacement. "
                "To change what one does, add a NEW migration with a UTC "
                "timestamp id."
            )

        # ...and the SAME rule for everything the enumerated list cannot hold.
        #
        # FROZEN_LEGACY_FILES is a hand-maintained snapshot of one closed set. It
        # cannot grow to cover timestamp migrations — those are unbounded and
        # nobody would remember to add them — so the moment the first timestamp
        # migration merges, a later PR can delete it or rename it to another
        # perfectly valid timestamp and every rule above still reports CLEAN.
        # Existing installs keep the old id in their ledger and skip the
        # replacement forever; fresh installs never run the deleted one. Same
        # divergence the legacy freeze exists to prevent, one namespace over.
        #
        # "Already merged" is a fact only git holds, so it is asked of git rather
        # than transcribed into a list that goes stale. This subsumes the check
        # above for anything on the base branch; the enumeration is kept because
        # it answers a different question — it asserts what main itself should
        # contain, and so still fires if the base branch is the thing that lost a
        # file.
        base_names = _runnable_names_at(repo_root, ref, rel_dir, ids)
        if base_names is None:
            if base_ref is not None:
                violations.append(
                    f"{label}s: base ref {ref!r} could not be read, so nothing "
                    "was compared against what is already merged. A guard that "
                    "was told to compare and then compared nothing is a silent "
                    "pass — fetch the base (actions/checkout needs "
                    "`fetch-depth: 0`) or drop --base to run without this rule."
                )
        else:
            gone = sorted(base_names - seen)
            if gone:
                violations.append(
                    f"{label}s: {len(gone)} migration(s) present at {ref} are "
                    f"missing here: {', '.join(gone)}. An already-merged "
                    "migration may not be deleted or renamed at any id — installs "
                    "that ran it keep its id in their ledger and will never run "
                    "the replacement, while a fresh install runs only the new "
                    "one. To change what a migration does, add a NEW one with a "
                    "UTC timestamp id."
                )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="repository root (default: this script's parent repo)",
    )
    parser.add_argument(
        "--base",
        default=None,
        help=(
            "git ref answering 'what is already merged' (CI passes the PR base "
            f"sha). Unset: try {_DEFAULT_BASE_REF} and skip the rule if it is "
            "not resolvable. Set: an unreadable ref is a violation."
        ),
    )
    args = parser.parse_args()

    try:
        violations = check(args.repo_root, base_ref=args.base)
    except Exception as exc:  # plumbing failure must never read as clean
        print(f"::error::migration-prefix guard could not run: {exc}", file=sys.stderr)
        # The annotation names the cause; the traceback is what a maintainer
        # needs when the cause is not self-describing.
        traceback.print_exc(file=sys.stderr)
        return 2

    if violations:
        for v in violations:
            print(f"::error::{v}")
        return 1
    # Two honest labels, neither overclaiming: with --base the comparison is
    # ENFORCED (an unreadable ref was a violation above); without it check()
    # still tried the default ref best-effort, silently skipping the rule if
    # that ref is absent — which is a weaker statement and must read as one.
    scope = (
        f"immutable against {args.base}"
        if args.base
        else f"immutability vs {_DEFAULT_BASE_REF} best-effort (pass --base to enforce)"
    )
    print(
        "Migration id check: CLEAN (every file classified; no duplicates; "
        f"frozen legacy set intact; {scope})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
