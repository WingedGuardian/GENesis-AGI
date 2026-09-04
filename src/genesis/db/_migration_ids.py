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

#: The frozen legacy window per namespace, as ``(lo, hi, count)`` — the exact
#: set that existed when the namespace froze (2026-09-03).
#:
#: BOTH ENDS are enforced, and the count with them. Below ``lo`` is as much a
#: hand-allocated id as above ``hi``: ``0000`` is legacy-width, duplicates
#: nothing, and slipped through an earlier one-sided ``prefix > hi`` rule — and
#: it is the WORST id for it, because on a fresh install it sorts first and runs
#: before ``0001``, while on an existing install every legacy id is already
#: applied so it runs last. Same tree, opposite order, nothing detecting it.
#:
#: ``count`` pins contiguity: for a contiguous range, ``(lo, hi, count)``
#: characterises the set exactly, so a DELETED legacy migration (which would
#: otherwise free its id for silent re-allocation) shows up as a violation
#: rather than as a gap. Moving the window is then a deliberate edit here.
FROZEN_LEGACY_WINDOW: dict[str, tuple[str, str, int]] = {
    "schema migration": ("0001", "0091", 91),
    "data migration": ("d0001", "d0011", 11),
}


def is_legacy_id(prefix: str) -> bool:
    """True for a hand-allocated 4-digit id (``0091``/``d0011``), not a timestamp."""
    digits = prefix[1:] if prefix.startswith("d") else prefix
    return len(digits) == 4
