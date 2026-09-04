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

Read-only. Exit 0 clean, 1 on any violation, 2 on a usage/plumbing error
(never a silent pass).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import traceback
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def check(repo_root: Path) -> list[str]:
    """Return a list of human-readable violations (empty == clean)."""
    ids = _load_id_contract(repo_root)
    surfaces = [
        (ids.SCHEMA, repo_root / "src/genesis/db/migrations"),
        (ids.DATA, repo_root / "src/genesis/db/data_migrations"),
    ]

    violations: list[str] = []
    for label, directory in surfaces:
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

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="repository root (default: this script's parent repo)",
    )
    args = parser.parse_args()

    try:
        violations = check(args.repo_root)
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
    print(
        "Migration id check: CLEAN (every file classified; no duplicates; "
        "frozen legacy set intact)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
