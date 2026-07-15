"""Shared file-discovery for the two migration runners.

Both the schema-migration runner (``db/migrations/runner.py``, files
``NNNN_desc.py``) and the data-migration runner (``db/data_migrations/
runner.py``, files ``dNNNN_desc.py``) discover numbered module files the same
way: sorted directory listing, regex match, ``group(1)`` is the stable id,
``path.stem`` is the display name. Only the filename PATTERN differs — the two
runners' EXECUTION semantics (atomic schema txn vs long-running idempotent
data backfill) are deliberately NOT shared.
"""

from __future__ import annotations

import re
from pathlib import Path


def discover_numbered_modules(
    directory: Path, pattern: re.Pattern[str]
) -> list[tuple[str, str, Path]]:
    """Return ``[(id, stem, path)]`` for files matching ``pattern``, id-ordered.

    ``pattern`` must capture the id in group 1 (e.g. ``r"^(\\d{4})_\\w+\\.py$"``
    or ``r"^(d\\d{4})_\\w+\\.py$"``). Sorted filename order == id order because
    the zero-padded numeric prefix sorts lexicographically.
    """
    out: list[tuple[str, str, Path]] = []
    for path in sorted(directory.iterdir()):
        m = pattern.match(path.name)
        if m:
            out.append((m.group(1), path.stem, path))
    return out
