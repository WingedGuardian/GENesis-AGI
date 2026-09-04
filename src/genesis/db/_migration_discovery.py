"""Shared file-discovery for the two migration runners.

Both the schema-migration runner (``db/migrations/runner.py``) and the
data-migration runner (``db/data_migrations/runner.py``, ``d``-prefixed)
discover numbered module files the same way: sorted directory listing, regex
match, ``group(1)`` is the stable id, ``path.stem`` is the display name. Only
the filename PATTERN differs — the two runners' EXECUTION semantics (atomic
schema txn vs long-running idempotent data backfill) are deliberately NOT
shared.

New migrations carry a UTC timestamp id (``YYYYMMDDHHMMSS``); the legacy
hand-allocated 4-digit ids are frozen. See ``db/migrations/runner.py`` for why.
"""

from __future__ import annotations

import re
from pathlib import Path


def discover_numbered_modules(
    directory: Path, pattern: re.Pattern[str]
) -> list[tuple[str, str, Path]]:
    """Return ``[(id, stem, path)]`` for files matching ``pattern``, id-ordered.

    ``pattern`` must capture the id in group 1 (the runners' own patterns
    accept a frozen 4-digit legacy id OR a 14-digit UTC timestamp).

    Sorted filename order == id order, and that holds ACROSS the two widths:
    every legacy id begins with ``0`` (the namespace froze at 0091) and every
    timestamp with ``2``, so lexicographic order puts the frozen history ahead
    of all new work. Pinned by
    ``tests/test_db/test_migration_discovery.py::test_frozen_legacy_ids_always_precede_timestamp_ids``.
    """
    out: list[tuple[str, str, Path]] = []
    for path in sorted(directory.iterdir()):
        m = pattern.match(path.name)
        if m:
            out.append((m.group(1), path.stem, path))
    return out
