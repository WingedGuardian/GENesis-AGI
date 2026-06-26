"""Direct unit tests for the ego_proposals read helpers used by the j9 aggregator.

These extract the date-windowed proposal queries the j9 metrics depend on;
the tests pin the window boundary ([start, end)), the column projections, and
the documented ``accepted is None`` edge (SUM over zero matching rows).
"""

from __future__ import annotations

import pytest

from genesis.db.crud import ego as ego_crud

pytestmark = pytest.mark.asyncio


async def _mk_proposal(
    db, *, id, created_at, status="pending", confidence=0.5,
    action_type="investigate", alternatives="", realist_verdict="pass",
):
    await db.execute(
        """INSERT INTO ego_proposals
           (id, action_type, content, status, confidence, alternatives,
            realist_verdict, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, action_type, "do the thing", status, confidence,
         alternatives, realist_verdict, created_at),
    )
    await db.commit()


_START = "2026-06-01T00:00:00+00:00"
_END = "2026-06-08T00:00:00+00:00"


# ── window boundary (shared by all three helpers) ────────────────────────

async def test_drift_window_is_half_open(db):
    """[start, end): start is inclusive, end is exclusive."""
    await _mk_proposal(db, id="before", created_at="2026-05-31T23:59:59+00:00")
    await _mk_proposal(db, id="at_start", created_at=_START)
    await _mk_proposal(db, id="inside", created_at="2026-06-04T12:00:00+00:00")
    await _mk_proposal(db, id="at_end", created_at=_END)  # excluded

    rows = await ego_crud.get_proposals_for_drift(db, start=_START, end=_END)
    assert len(rows) == 2  # at_start + inside; before/at_end excluded
    assert set(rows[0].keys()) == {"action_type", "alternatives", "realist_verdict"}


# ── get_acceptance_counts ────────────────────────────────────────────────

async def test_acceptance_counts_excludes_pending_and_expired(db):
    await _mk_proposal(db, id="p1", created_at=_START, status="pending")
    await _mk_proposal(db, id="p2", created_at=_START, status="expired")
    await _mk_proposal(db, id="p3", created_at=_START, status="approved")
    await _mk_proposal(db, id="p4", created_at=_START, status="executed")
    await _mk_proposal(db, id="p5", created_at=_START, status="rejected")

    counts = await ego_crud.get_acceptance_counts(db, start=_START, end=_END)
    assert counts["total"] == 3  # approved + executed + rejected
    assert counts["accepted"] == 2  # approved + executed


async def test_acceptance_counts_accepted_none_when_no_rows(db):
    """SUM over zero matching rows yields NULL → ``accepted`` is None, total 0."""
    counts = await ego_crud.get_acceptance_counts(db, start=_START, end=_END)
    assert counts["total"] == 0
    assert counts["accepted"] is None


# ── get_proposals_for_quality ────────────────────────────────────────────

async def test_quality_projection_and_window(db):
    await _mk_proposal(db, id="q_out", created_at="2026-05-01T00:00:00+00:00")
    await _mk_proposal(
        db, id="q_in", created_at="2026-06-03T00:00:00+00:00",
        status="approved", confidence=0.9,
    )
    rows = await ego_crud.get_proposals_for_quality(db, start=_START, end=_END)
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"id", "status", "confidence", "action_type"}
    assert rows[0]["id"] == "q_in"
    assert rows[0]["confidence"] == 0.9
