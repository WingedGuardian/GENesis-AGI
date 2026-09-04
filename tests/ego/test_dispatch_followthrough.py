"""Tests for dispatch follow-through — ego_intentions rows forcing outcome review.

Every terminal ego dispatch creates a system-origin intention for the owning
ego so the next cycle MUST review the outcome (step-2, retry, or close).
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

PROPOSAL_ID = "abc12345deadbeef"


@pytest.fixture()
async def db():
    """In-memory DB with production ego_proposals + ego_intentions schemas."""
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        await conn.execute(TABLES["ego_intentions"])
        await conn.commit()
        yield conn


async def _seed_proposal(db, *, ego_source: str | None, pid: str = PROPOSAL_ID):
    await db.execute(
        """INSERT INTO ego_proposals
           (id, action_type, content, status, created_at, ego_source)
           VALUES (?, 'implement', 'Ship the ingestion retry fix', 'executed', ?, ?)""",
        (pid, datetime.now(UTC).isoformat(), ego_source),
    )
    await db.commit()


class TestRecordDispatchFollowthrough:
    @pytest.mark.asyncio
    async def test_creates_intention_for_owning_ego(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await _seed_proposal(db, ego_source="user_ego_cycle")
        iid = await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="completed",
            outcome="PR opened and merged",
            failed=False,
        )
        assert iid is not None
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 1
        row = items[0]
        assert row["origin"] == "system"
        assert row["proposal_id"] == PROPOSAL_ID
        assert row["max_cycles"] == 3
        assert row["priority"] == "normal"
        # Content carries what the ego needs to judge follow-through — status +
        # the ego's own (first-party) proposal summary — but NOT the raw session
        # outcome (that would launder external_untrusted content into the
        # privileged mandatory-review block; see test_outcome_text_not_laundered).
        assert PROPOSAL_ID[:8] in row["content"]
        assert "completed" in row["content"]
        assert "Ship the ingestion retry fix" in row["content"]
        assert "PR opened and merged" not in row["content"]

    @pytest.mark.asyncio
    async def test_null_ego_source_falls_back_to_genesis(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await _seed_proposal(db, ego_source=None)
        iid = await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="completed",
            outcome="",
            failed=False,
        )
        assert iid is not None
        items = await ego_intentions.list_active(db, "genesis_ego_cycle")
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_failed_dispatch_gets_high_priority(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await _seed_proposal(db, ego_source="genesis_ego_cycle")
        await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="completed",
            outcome="Tests failed: 3 errors in test_routing",
            failed=True,
        )
        items = await ego_intentions.list_active(db, "genesis_ego_cycle")
        assert items[0]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_dedup_second_dispatch_same_proposal(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await _seed_proposal(db, ego_source="user_ego_cycle")
        first = await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1111aaaa",
            status="completed",
            outcome="run 1",
            failed=False,
        )
        second = await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess2222bbbb",
            status="completed",
            outcome="run 2",
            failed=False,
        )
        assert first is not None
        assert second is None
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 1

    @pytest.mark.asyncio
    async def test_missing_proposal_returns_none(self, db):
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        iid = await record_dispatch_followthrough(
            db,
            proposal_id="does0not0exist00",
            session_id="sess1234abcd",
            status="completed",
            outcome="",
            failed=False,
        )
        assert iid is None

    @pytest.mark.asyncio
    async def test_created_even_when_llm_cap_full(self, db):
        """The keystone must not silently drop when the ego's board is busy."""
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await _seed_proposal(db, ego_source="user_ego_cycle")
        for i in range(ego_intentions.MAX_ACTIVE_PER_SOURCE):
            assert await ego_intentions.create(
                db,
                content=f"Intention {i}",
                trigger_condition=f"Trigger {i}",
                ego_source="user_ego_cycle",
            )
        iid = await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="completed",
            outcome="done",
            failed=False,
        )
        assert iid is not None

    @pytest.mark.asyncio
    async def test_outcome_text_not_laundered_into_content(self, db):
        """The raw dispatch outcome must NEVER reach the intention content — it
        lands in the ego's privileged mandatory-review block, and a research/
        interact dispatch's output is external_untrusted (Codex #1496 P2)."""
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await _seed_proposal(db, ego_source="user_ego_cycle")
        await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="completed",
            outcome="OUTCOME_SENTINEL_XYZ web-sourced text",
            failed=False,
        )
        row = (await ego_intentions.list_active(db, "user_ego_cycle"))[0]
        assert "OUTCOME_SENTINEL_XYZ" not in row["content"]

    @pytest.mark.asyncio
    async def test_verification_failed_proposal_gets_high_priority(self, db):
        """A dispatch whose SESSION completed but whose proposal was marked failed
        (post-dispatch verification) must be treated as failed → high priority,
        even though the caller's session-derived flag says failed=False (#1496 P2)."""
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        # Proposal marked failed by verification, but the session "completed".
        await db.execute(
            """INSERT INTO ego_proposals
               (id, action_type, content, status, created_at, ego_source)
               VALUES (?, 'implement', 'Ship X', 'failed', ?, 'genesis_ego_cycle')""",
            (PROPOSAL_ID, datetime.now(UTC).isoformat()),
        )
        await db.commit()
        await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="completed",
            outcome="",
            failed=False,
        )
        row = (await ego_intentions.list_active(db, "genesis_ego_cycle"))[0]
        assert row["priority"] == "high"

    @pytest.mark.asyncio
    async def test_caption_renders_derived_failed_not_raw_status(self, db):
        """A verification-failed-but-session-completed dispatch must render the
        DERIVED [failed] in the review caption, not the misleading raw
        [completed] (#1496 P2 dispatch_followthrough.py:95)."""
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await db.execute(
            """INSERT INTO ego_proposals
               (id, action_type, content, status, created_at, ego_source)
               VALUES (?, 'implement', 'Ship X', 'failed', ?, 'genesis_ego_cycle')""",
            (PROPOSAL_ID, datetime.now(UTC).isoformat()),
        )
        await db.commit()
        await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="completed",  # session completed, but proposal verification-failed
            outcome="",
            failed=False,
        )
        row = (await ego_intentions.list_active(db, "genesis_ego_cycle"))[0]
        assert "[failed]" in row["content"], row["content"]
        assert "[completed]" not in row["content"], row["content"]

    @pytest.mark.asyncio
    async def test_parked_dispatch_skips_followthrough(self, db):
        """A rate/quota-parked dispatch is NOT terminal — no follow-through now;
        the resumed session's on_end fires the real one (Codex #1496 P2)."""
        from genesis.db.crud import ego_intentions
        from genesis.ego.dispatch_followthrough import record_dispatch_followthrough

        await _seed_proposal(db, ego_source="user_ego_cycle")
        iid = await record_dispatch_followthrough(
            db,
            proposal_id=PROPOSAL_ID,
            session_id="sess1234abcd",
            status="failed",
            outcome="rate_limited: parked for resume",
            failed=True,
            parked=True,
        )
        assert iid is None
        assert await ego_intentions.list_active(db, "user_ego_cycle") == []
