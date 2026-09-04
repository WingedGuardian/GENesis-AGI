#!/usr/bin/env python3
"""Migration-id guard: no duplicate prefixes, and the legacy namespace stays frozen.

Replaces the shell glob+grep that lived in ci.yml. That version could not
survive the move to timestamp ids in either direction, both measured
2026-09-03 against a mixed directory:

  * its glob ``[0-9][0-9][0-9][0-9]_*.py`` made every timestamp migration
    INVISIBLE to the duplicate check — the guard silently stopped guarding;
  * widening the glob while keeping ``grep -oP '^\\d{4}'`` truncated
    ``20260903200000`` to ``2026``, so any two same-year migrations read as a
    duplicate of each other — CI red on the second one ever written.

Both failures come from re-implementing the runners' filename semantics in a
second language. This script instead IMPORTS the authoritative patterns from
``src/genesis/db/_migration_ids.py``, so the guard cannot drift from what
actually runs (the same reason the tree-wide test imports them rather than
copying).

It imports that module and NOT the runners, which was the first attempt: the
runners carry module-scope ``import aiosqlite`` / ``from genesis.db.crud …``,
so binding to them pulled 117 modules into a CI job that installs nothing and
exited 2 on every PR — a guard that enforced neither rule while every test
passed, because pytest runs inside the project venv. ``_migration_ids`` is
stdlib-only precisely so this path import is safe under a bare ``python3``;
``test_ci_command_line_*`` runs this script the way CI does so that premise
stays true.

Two rules, both fail-closed:

1. **No duplicate prefix** in either namespace. Both runners raise on one —
   the schema runner aborts bootstrap on every install, the data runner skips
   its whole batch post-boot — so this stays enforced for timestamp ids too
   (two authors can hit the same second, however unlikely).
2. **The legacy namespace is FROZEN.** New migrations use a UTC timestamp id;
   a new 4-digit (or ``d`` + 4-digit) prefix is refused. Freezing is what
   removes prefix contention at the root: nobody allocates an id any more, so
   two branches cannot claim the same one. Enforced against the frozen
   window's BOTH ENDS plus its size — an earlier one-sided "above the mark"
   rule let ``0000`` through, which is legacy-width, duplicates nothing, and
   is the worst possible id for it (fresh install: runs before ``0001``;
   existing install: runs after ``0091``).

Read-only. Exit 0 clean, 1 on any violation, 2 on a usage/plumbing error
(never a silent pass).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import traceback
from collections import defaultdict
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
        (
            "schema migration",
            repo_root / "src/genesis/db/migrations",
            ids.SCHEMA_MIGRATION_PATTERN,
        ),
        (
            "data migration",
            repo_root / "src/genesis/db/data_migrations",
            ids.DATA_MIGRATION_PATTERN,
        ),
    ]

    violations: list[str] = []
    for label, directory, pattern in surfaces:
        if not directory.is_dir():
            # Fail CLOSED: the id contract now lives OUTSIDE these directories,
            # so a missing one no longer fails at load — and a scan that never
            # read a directory must never read as clean.
            violations.append(
                f"{label}s: directory not found at {directory} — nothing was scanned"
            )
            continue

        by_prefix: dict[str, list[str]] = defaultdict(list)
        for path in sorted(directory.iterdir()):
            m = pattern.match(path.name)
            if m:
                by_prefix[m.group(1)].append(path.name)

        if not by_prefix:
            violations.append(
                f"{label}s: no files matched {pattern.pattern} in {directory} — "
                "not a possible state, so the scan did not describe the directory"
            )
            continue

        for prefix, names in sorted(by_prefix.items()):
            if len(names) > 1:
                violations.append(
                    f"{label}s: duplicate prefix '{prefix}' — {', '.join(sorted(names))}. "
                    "Rename one to a fresh UTC timestamp id "
                    "(`date -u +%Y%m%d%H%M%S`)."
                )

        lo, hi, expected = ids.FROZEN_LEGACY_WINDOW[label]
        suffix = " with a leading 'd'." if label == "data migration" else "."
        legacy = sorted(p for p in by_prefix if ids.is_legacy_id(p))

        for prefix in legacy:
            # BOTH ends: below `lo` is as much a hand-allocated id as above
            # `hi`, and `0000` — which a one-sided rule let through — is the
            # id whose fresh-vs-existing ordering diverges maximally.
            if not (lo <= prefix <= hi):
                violations.append(
                    f"{label}s: '{prefix}' ({', '.join(sorted(by_prefix[prefix]))}) "
                    f"allocates a NEW legacy-width id, but that namespace is frozen "
                    f"at {lo}..{hi}. New migrations use a UTC timestamp id: "
                    "`date -u +%Y%m%d%H%M%S`_description.py" + suffix
                )

        # Contiguity: for a contiguous range, (lo, hi, count) characterises the
        # set exactly — so a DELETED legacy migration, which would otherwise
        # free its id for silent re-allocation, surfaces here instead.
        if legacy and (legacy[0], legacy[-1], len(legacy)) != (lo, hi, expected):
            violations.append(
                f"{label}s: the frozen legacy window {lo}..{hi} should hold exactly "
                f"{expected} ids; found {len(legacy)} spanning "
                f"{legacy[0]}..{legacy[-1]}. A deleted legacy migration frees its id "
                "for re-allocation — restore it, or move the window deliberately in "
                "src/genesis/db/_migration_ids.py."
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
    print("Migration prefix check: CLEAN (no duplicates; legacy namespace frozen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
