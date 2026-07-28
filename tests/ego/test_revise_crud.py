"""PR-5 unit B — revision CRUD: revise_proposal (guard-first versioned revise),
reaffirm_proposal, withdraw-with-evidence. Wall-clock-independent."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.integrity import content_hash, content_size


@pytest.fixture
async def db():
    """In-memory DB with the proposal board + its revision audit table."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        await conn.execute(TABLES["ego_proposal_revisions"])
        yield conn


async def _seed(db, *, id="p1", content="original content", status="pending"):
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type="investigate",
        content=content,
        rationale="orig rationale",
        confidence=0.7,
        status=status,
        execution_plan="orig plan",
        expected_outputs="orig outputs",
        content_hash=content_hash(content),
        content_size=content_size(content),
    )


async def _row(db, id="p1"):
    return await ego_crud.get_proposal(db, id)


# ── reaffirm ─────────────────────────────────────────────────────────────


async def test_reaffirm_sets_last_validated(db):
    await _seed(db)
    assert (await _row(db))["last_validated_at"] is None
    assert await ego_crud.reaffirm_proposal(db, "p1") is True
    assert (await _row(db))["last_validated_at"] is not None


async def test_reaffirm_noop_on_non_pending(db):
    await _seed(db, status="approved")
    assert await ego_crud.reaffirm_proposal(db, "p1") is False
    assert (await _row(db))["last_validated_at"] is None


# ── revise (happy path) ──────────────────────────────────────────────────


async def test_revise_bumps_version_and_recomputes_hash(db):
    await _seed(db)
    new_rev = await ego_crud.revise_proposal(
        db,
        "p1",
        expected_revision=1,
        content="revised content — bigger premise",
        rationale="new rationale",
        confidence=0.9,
        execution_plan="new plan",
        expected_outputs="new outputs",
        revised_by="reconcile",
        reason="premise sharpened",
    )
    assert new_rev == 2
    row = await _row(db)
    assert row["revision_num"] == 2
    assert row["content"] == "revised content — bigger premise"
    assert row["rationale"] == "new rationale"
    assert row["confidence"] == 0.9
    assert row["content_hash"] == content_hash("revised content — bigger premise")
    assert row["content_size"] == content_size("revised content — bigger premise")
    assert row["last_validated_at"] is not None
    # action_type is immutable
    assert row["action_type"] == "investigate"


async def test_revise_writes_prior_values_to_audit(db):
    await _seed(db)
    await ego_crud.revise_proposal(
        db, "p1", expected_revision=1, content="v2 content", revised_by="reconcile", reason="why"
    )
    cur = await db.execute("SELECT * FROM ego_proposal_revisions WHERE proposal_id = ?", ("p1",))
    revs = [dict(r) for r in await cur.fetchall()]
    assert len(revs) == 1
    audit = revs[0]
    # The audit row holds the SUPERSEDED version's values under revision_num=1.
    assert audit["revision_num"] == 1
    assert audit["content"] == "original content"
    assert audit["rationale"] == "orig rationale"
    assert audit["execution_plan"] == "orig plan"
    assert audit["revised_by"] == "reconcile"
    assert audit["reason"] == "why"
    assert audit["revised_at"] is not None


async def test_revise_chain_two_revisions(db):
    await _seed(db)
    assert await ego_crud.revise_proposal(db, "p1", expected_revision=1, content="v2") == 2
    assert await ego_crud.revise_proposal(db, "p1", expected_revision=2, content="v3") == 3
    assert (await _row(db))["revision_num"] == 3
    cur = await db.execute(
        "SELECT revision_num FROM ego_proposal_revisions WHERE proposal_id = ? "
        "ORDER BY revision_num",
        ("p1",),
    )
    assert [r["revision_num"] for r in await cur.fetchall()] == [1, 2]


# ── revise (guard failures) ──────────────────────────────────────────────


async def test_revise_stale_revision_refused(db):
    await _seed(db)
    # Caller thinks it's at rev 1, but the live row is already at rev 2.
    await ego_crud.revise_proposal(db, "p1", expected_revision=1, content="v2")
    result = await ego_crud.revise_proposal(db, "p1", expected_revision=1, content="racing v2b")
    assert result is None
    row = await _row(db)
    assert row["content"] == "v2"  # unchanged by the losing revise
    assert row["revision_num"] == 2
    # No orphan audit row from the refused revise (guard-first).
    cur = await db.execute("SELECT COUNT(*) c FROM ego_proposal_revisions")
    assert (await cur.fetchone())["c"] == 1


async def test_revise_refused_on_non_pending(db):
    await _seed(db, status="approved")
    assert await ego_crud.revise_proposal(db, "p1", expected_revision=1, content="v2") is None
    assert (await _row(db))["content"] == "original content"
    cur = await db.execute("SELECT COUNT(*) c FROM ego_proposal_revisions")
    assert (await cur.fetchone())["c"] == 0


async def test_revise_absent_id_returns_none(db):
    assert await ego_crud.revise_proposal(db, "nope", expected_revision=1, content="v2") is None


# ── withdraw with evidence ───────────────────────────────────────────────


async def test_withdraw_with_evidence_records_note(db):
    await _seed(db)
    assert await ego_crud.withdraw_proposal(db, "p1", user_response="premise overtaken") is True
    row = await _row(db)
    assert row["status"] == "withdrawn"
    assert row["rank"] is None
    assert row["user_response"] == "premise overtaken"


async def test_withdraw_without_note_is_backward_compatible(db):
    await _seed(db)
    assert await ego_crud.withdraw_proposal(db, "p1") is True
    row = await _row(db)
    assert row["status"] == "withdrawn"
    assert row["user_response"] is None


async def test_withdraw_coalesce_preserves_existing_note(db):
    await _seed(db)
    # A prior note exists on the row; a None withdraw must not clobber it.
    await db.execute(
        "UPDATE ego_proposals SET user_response = 'earlier note' WHERE id = ?", ("p1",)
    )
    await db.commit()
    await ego_crud.withdraw_proposal(db, "p1", user_response=None)
    assert (await _row(db))["user_response"] == "earlier note"
