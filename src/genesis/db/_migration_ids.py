"""The migration-id contract: what ids are legal, in one place.

**STDLIB-ONLY, ON PURPOSE — do not add an import to this module.**
``scripts/check_migration_prefixes.py`` path-imports it from a CI job that runs
a bare ``python3`` with no ``setup-python`` and no ``pip install``. An earlier
version of that guard imported the two RUNNER modules instead, to bind to their
real patterns; the intent was right but the runners carry module-scope
``import aiosqlite`` / ``from genesis.db.crud import …``, so the guard pulled in
117 modules and exited 2 on every PR — enforcing nothing, while every test
passed because pytest runs inside the project venv. Two regexes have no reason
to drag the package in; this module is what both sides can safely share.

New migrations use a UTC timestamp id (``date -u +%Y%m%d%H%M%S``). The legacy
hand-allocated 4-digit ids are FROZEN — see ``migrations/runner.py`` for why
allocation was the thing worth removing.
"""

from __future__ import annotations

import re

#: Schema migrations: a frozen 4-digit legacy id OR a 14-digit UTC timestamp.
#: Width-anchored alternation (``{4}|{14}``, never ``{4,14}``) so a mistyped
#: 13- or 15-digit id is REFUSED at discovery rather than quietly founding a
#: third id namespace with its own ordering semantics.
SCHEMA_MIGRATION_PATTERN = re.compile(r"^(\d{4}|\d{14})_\w+\.py$")

#: Data migrations: the same, ``d``-prefixed. The prefix keeps the two
#: namespaces disjoint, so they can never cross-collide in one claims map.
DATA_MIGRATION_PATTERN = re.compile(r"^(d(?:\d{4}|\d{14}))_\w+\.py$")

#: The frozen legacy window per namespace, as ``(lo, hi)`` — the range of
#: hand-allocated ids that already exist. A legacy-width id OUTSIDE it is a new
#: allocation and is refused.
#:
#: BOTH ENDS matter. Below ``lo`` is as much a hand-allocated id as above
#: ``hi``: ``0000`` is legacy-width, duplicates nothing, and slipped an earlier
#: one-sided ``prefix > hi`` rule — and it is the worst id to miss, because on
#: a fresh install it sorts first and runs before ``0001`` while on an existing
#: install every legacy id is already applied, so it runs last. Same tree,
#: opposite order, nothing detecting it.
#:
#: TRANSITION CONSTANT: ``hi`` tracks the highest legacy id on the default
#: branch, which still moves while migrations authored under the old
#: convention are in flight (``0093`` landed 2026-09-04, leaving a gap at
#: ``0092`` from a rename). If one merges above ``hi`` before this scheme does,
#: bump it here — the guard will say exactly that. Once the in-flight set is
#: drained the window is fixed forever, because nothing allocates a legacy id
#: any more.
#:
#: Deliberately NOT pinned by count. A size check would catch a DELETED legacy
#: migration freeing its id — but the freeze already makes that unreachable
#: (every new migration is a timestamp, so no id is ever re-allocated), and the
#: range is not contiguous anyway.
FROZEN_LEGACY_WINDOW: dict[str, tuple[str, str]] = {
    "schema migration": ("0001", "0093"),
    "data migration": ("d0001", "d0011"),
}


def is_legacy_id(prefix: str) -> bool:
    """True for a hand-allocated 4-digit id (``0091``/``d0011``), not a timestamp."""
    digits = prefix[1:] if prefix.startswith("d") else prefix
    return len(digits) == 4
