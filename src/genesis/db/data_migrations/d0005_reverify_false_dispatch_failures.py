"""d0005 — un-bury dispatch proposals false-failed by the old string/size gate.

Post-dispatch verification used to hard-fail a proposal (status → 'failed')
when a ``required_strings`` entry was absent, a file was under
``min_size_bytes``, or a ``~``/``$VAR`` path was left unexpanded. This
false-failed complete deliverables and fed a NEGATIVE learning signal into the
feedback harvester for work that actually succeeded. The mechanism is fixed
going forward (``ego/verification.py``: existence-only hard signal, string/size
advisory, path expansion). This migration repairs the rows already stranded as
'failed' on existing installs.

Re-verify every proposal sitting at status='failed' with a
``|verification_failed:`` marker using the fixed logic. Flip to 'executed'
(appending a ``|completed:`` suffix so the outcome reads as the success it was)
ONLY when the deliverable now passes AND every expected file exists at its
EXACT resolved path — never on a fuzzy-name match, which no heuristic can
reliably tell from an unrelated similarly-named file (a wrong file scored higher
name-similarity than two legit renames in the observed data). Fuzzy-only and
genuinely-missing rows are left 'failed' — conservative by design.

KNOWN LIMITATION (tracked as a follow-up, not fixed here): the negative
``ln`` (Outcome Bus) EXECUTION_OUTCOME already harvested for these proposals is
keyed on a unique (source, ref_type, ref_id, signal_type) and will not be
overwritten by a re-harvest. This migration corrects proposal STATUS (dashboard
visibility + ``capability_aggregator`` counts), not the already-recorded
learning signal.

migrate()/verify() are SYNC (framework contract, cf. d0001/d0002/d0004); own
connections only — never the runtime's async ``rt._db``. Idempotent: once a row
is 'executed' it no longer matches the status='failed' filter, and a fresh
install has no such rows.
"""

from __future__ import annotations

import sqlite3

from genesis.ego.verification import (
    _resolve_path,
    parse_expected_outputs,
    verify_outputs,
)
from genesis.env import genesis_db_path

requires_operator = False

_SELECT = (
    "SELECT id, user_response, expected_outputs FROM ego_proposals "
    "WHERE status = 'failed' AND user_response LIKE '%verification_failed%'"
)

_NOTE = (
    "|completed:re-verified under fixed advisory logic (d0005 data-migration); "
    "deliverable present at the exact expected path"
)


def _exact_pass(expected_outputs: str | None) -> bool:
    """True iff the deliverable now passes AND every file exists at its exact
    resolved path (no fuzzy substitution). Conservative: any parse/IO problem
    or fuzzy-only match returns False (leave the row 'failed')."""
    expected = parse_expected_outputs(expected_outputs)
    if expected is None:
        return False
    try:
        if not all(_resolve_path(f).exists() for f in expected.files):
            return False
        return verify_outputs(expected).passed
    except OSError:
        return False


def migrate() -> dict:
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:
        rows = db.execute(_SELECT).fetchall()
        flipped = 0
        for pid, _user_response, expected_outputs in rows:
            if not _exact_pass(expected_outputs):
                continue
            db.execute(
                "UPDATE ego_proposals SET status = 'executed', "
                "user_response = COALESCE(user_response, '') || ? "
                "WHERE id = ? AND status = 'failed'",
                (_NOTE, pid),
            )
            flipped += 1
        db.commit()
        return {"flipped": flipped, "scanned": len(rows)}
    finally:
        db.close()


def verify() -> bool:
    """Complete when no exact-pass proposal remains stranded at 'failed'."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        rows = db.execute(_SELECT).fetchall()
    finally:
        db.close()
    return not any(_exact_pass(eo) for _pid, _ur, eo in rows)
