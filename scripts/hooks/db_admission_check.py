"""Hooks-side shim over :mod:`genesis.db.admission` — the fence check every
script-side raw SQLite opener consults before connecting.

Why a shim instead of each script importing ``genesis.db.admission`` directly:
the exception POLICY belongs in one place. Hook processes must never crash a
session over fence bookkeeping, and the fail direction must be uniform — any
failure to establish state (src tree not importable, unexpected error) reads
as FENCED, because every caller's designed degrade is "skip the database this
once" and a skipped advisory write is recoverable where a write to a fenced
database is the 2026-09-18 incident class. The predicate itself lives in
``src/genesis/db/admission.py``; this file adds only import plumbing and the
policy — one implementation, no drift.

Import pattern: scripts under ``scripts/`` already insert ``scripts/hooks`` on
``sys.path`` to share hook helpers (see proactive_memory_hook.py:41-42); files
in this directory import it as a sibling.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"


def database_is_fenced(db_path) -> bool:
    """True when *db_path* must not be touched (quarantine).

    Fail-closed wrapper: an import failure or any unexpected error returns
    True. A one-line stderr note is emitted for the journal on the abnormal
    path only — CC discards an exit-0 hook's stderr, so this never reaches
    the model or the user mid-session.
    """
    try:
        if str(_SRC_DIR) not in sys.path:
            sys.path.insert(0, str(_SRC_DIR))
        from genesis.db.admission import database_is_fenced as _fenced

        return _fenced(db_path)
    except Exception as exc:  # noqa: BLE001 - policy: never crash a hook here
        print(
            f"db_admission_check: could not establish fence state ({exc!r}) — "
            "treating the database as fenced (advisory skip)",
            file=sys.stderr,
        )
        return True
