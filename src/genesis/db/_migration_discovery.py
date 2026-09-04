"""Shared file-discovery for the two migration runners.

Both the schema-migration runner (``db/migrations/runner.py``) and the
data-migration runner (``db/data_migrations/runner.py``, ``d``-prefixed)
discover numbered module files the same way: sorted directory listing,
classification against the id contract, ``group(1)`` is the stable id,
``path.stem`` is the display name. Only the NAMESPACE differs — the two
runners' EXECUTION semantics (atomic schema txn vs long-running idempotent data
backfill) are deliberately NOT shared.

New migrations carry a UTC timestamp id (``YYYYMMDDHHMMSS``); this repo's
legacy hand-allocated 4-digit ids are frozen. See ``db/migrations/runner.py``.

This function takes a NAMESPACE, not a pattern. It used to take the pattern,
which let a caller pass one that disagreed with the directory it was scanning —
and, more importantly, made the pattern the whole contract, so a filename it did
not match was skipped in silence.

WHAT THE RUNTIME ENFORCES, AND WHAT IT DELIBERATELY DOES NOT
------------------------------------------------------------
The runtime refuses on files that CANNOT RUN, and only those. A file that is
merely disallowed by this repo's freeze — a fork's own ``0094_fork_custom.py``,
say — is well-formed, has a unique id, sorts correctly, and applies cleanly. It
breaks a policy of ours, not an invariant of theirs.

That distinction is load-bearing because this discovery sits on the boot-abort
path: ``runtime/init/db.py`` re-raises, and ``runtime/_core.py`` stops bootstrap
when the DB step fails. Enforcing OUR namespace policy there would leave any
downstream fork with no database after a pull, over a 4-digit id that was the
only convention available before this scheme existed. So the runtime logs a
warning and runs it; ``scripts/check_migration_prefixes.py`` refuses it in CI,
which is where a rule about this repo's namespace belongs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.db._migration_ids import scan_directory

logger = logging.getLogger(__name__)


def discover_numbered_modules(directory: Path, namespace: str) -> list[tuple[str, str, Path]]:
    """Return ``[(id, stem, path)]`` for the migrations in ``directory``, id-ordered.

    ``namespace`` is ``_migration_ids.SCHEMA`` or ``.DATA``.

    Raises ``RuntimeError`` if any entry PRESENTS as a migration but CANNOT run
    — a mistyped width (``2026090320000_x.py``), a ``d`` prefix in the wrong
    directory, a non-ASCII digit. Refusing is the whole point: the previous
    version matched a regex and skipped whatever failed, so a mistyped migration
    was discovered by nothing and NEVER RAN, and the code that needed it
    deployed against a schema that never got the change. A duplicate id already
    aborts here for a strictly smaller reason (``migrations/runner.py``
    pre-flight), and a duplicate is at least loud; this one was silent.

    Sorted filename order == id order, and that holds ACROSS the two widths:
    every frozen legacy id begins with ``0`` and every timestamp id is a real
    calendar year >= ``TIMESTAMP_EPOCH_YEAR`` and so begins with ``2``, which is
    what makes lexicographic order put the frozen history ahead of all new work.
    Pinned by
    ``tests/test_db/test_migration_discovery.py::test_frozen_legacy_ids_always_precede_timestamp_ids``.
    """
    runnable, unrunnable, disallowed = scan_directory(namespace, directory)

    if unrunnable:
        raise RuntimeError(
            f"{directory} contains {len(unrunnable)} unrunnable {namespace} "
            f"file(s); refusing to run any migration from this directory "
            f"because a file that is discovered by nothing fails SILENTLY:\n  "
            + "\n  ".join(unrunnable)
        )

    for note in disallowed:
        # Runs anyway — see the module docstring. CI is where this is refused.
        logger.warning("%s outside this repo's frozen set: %s", namespace, note)

    return runnable
