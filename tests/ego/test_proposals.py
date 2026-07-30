"""Tests for the ego proposal workflow."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.proposals import ProposalWorkflow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """In-memory DB with ego tables."""
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        await conn.execute(TABLES["ego_state"])
        yield conn


@pytest.fixture
def mock_topic_manager():
    """TopicManager that returns a canned delivery_id."""
    tm = AsyncMock()
    tm.send_to_category.return_value = "msg12345"
    return tm


@pytest.fixture
def mock_memory_store():
    """MemoryStore mock for correction storage tests."""
    ms = AsyncMock()
    ms.store.return_value = "mem_123"
    return ms


@pytest.fixture
def workflow(db, mock_topic_manager, mock_memory_store):
    return ProposalWorkflow(
        db=db,
        topic_manager=mock_topic_manager,
        memory_store=mock_memory_store,
    )


def _sample_proposals(n: int = 3) -> list[dict]:
    """Generate N sample proposal dicts."""
    samples = [
        {
            "action_type": "investigate",
            "action_category": "system_health",
            "content": "Check why observation backlog grew 3x",
            "rationale": "Backlog at 47 unresolved vs 15 yesterday",
            "confidence": 0.85,
            "urgency": "normal",
            "alternatives": "Wait for reflection to catch it",
        },
        {
            "action_type": "outreach",
            "action_category": "communication",
            "content": "Send weekly summary to user",
            "rationale": "7 days since last strategic report",
            "confidence": 0.70,
            "urgency": "low",
        },
        {
            "action_type": "maintenance",
            "action_category": "infrastructure",
            "content": "Run code audit on outreach pipeline",
            "rationale": "3 delivery failures in 24h",
            "confidence": 0.60,
            "urgency": "high",
            "alternatives": "Surplus could check cheaper",
        },
    ]
    return samples[:n]


# ---------------------------------------------------------------------------
# Proposal CRUD tests
# ---------------------------------------------------------------------------


class TestProposalCRUD:
    async def test_create_and_get_roundtrip(self, db):
        pid = await ego_crud.create_proposal(
            db, id="p1", action_type="investigate", content="test",
        )
        assert pid == "p1"
        row = await ego_crud.get_proposal(db, "p1")
        assert row is not None
        assert row["action_type"] == "investigate"
        assert row["status"] == "pending"

    async def test_get_missing(self, db):
        assert await ego_crud.get_proposal(db, "nope") is None

    async def test_list_by_batch(self, db):
        for i in range(3):
            await ego_crud.create_proposal(
                db, id=f"p{i}", action_type="test", content=f"c{i}",
                batch_id="batch1",
            )
        await ego_crud.create_proposal(
            db, id="other", action_type="test", content="other",
            batch_id="batch2",
        )
        rows = await ego_crud.list_proposals_by_batch(db, "batch1")
        assert len(rows) == 3
        assert [r["id"] for r in rows] == ["p0", "p1", "p2"]

    async def test_list_pending(self, db):
        await ego_crud.create_proposal(
            db, id="p1", action_type="t", content="c",
            created_at="2026-01-01",
        )
        await ego_crud.create_proposal(
            db, id="p2", action_type="t", content="c",
            created_at="2026-01-02",
        )
        await ego_crud.resolve_proposal(db, "p1", status="approved")
        rows = await ego_crud.list_pending_proposals(db)
        assert len(rows) == 1
        assert rows[0]["id"] == "p2"

    async def test_resolve_proposal(self, db):
        await ego_crud.create_proposal(
            db, id="p1", action_type="t", content="c",
        )
        ok = await ego_crud.resolve_proposal(
            db, "p1", status="approved", user_response="looks good",
        )
        assert ok is True
        row = await ego_crud.get_proposal(db, "p1")
        assert row["status"] == "approved"
        assert row["user_response"] == "looks good"
        assert row["resolved_at"] is not None

    async def test_resolve_nonexistent(self, db):
        ok = await ego_crud.resolve_proposal(db, "nope", status="approved")
        assert ok is False

    async def test_resolve_already_resolved(self, db):
        """Can't resolve an already-resolved proposal."""
        await ego_crud.create_proposal(
            db, id="p1", action_type="t", content="c",
        )
        await ego_crud.resolve_proposal(db, "p1", status="approved")
        ok = await ego_crud.resolve_proposal(db, "p1", status="rejected")
        assert ok is False

    async def test_resolve_expected_revision_absent_behaves_as_today(self, db):
        """PR-4 TOCTOU guard is inert by default: no expected_revision resolves."""
        await ego_crud.create_proposal(db, id="p1", action_type="t", content="c")
        ok = await ego_crud.resolve_proposal(db, "p1", status="approved")
        assert ok is True
        row = await ego_crud.get_proposal(db, "p1")
        assert row["status"] == "approved"

    async def test_resolve_expected_revision_match_updates(self, db):
        """Matching revision_num -> the guarded update applies."""
        await ego_crud.create_proposal(db, id="p1", action_type="t", content="c")
        # Fresh proposals default to revision_num = 1 (ego_proposals DEFAULT).
        ok = await ego_crud.resolve_proposal(
            db, "p1", status="approved", expected_revision=1,
        )
        assert ok is True
        row = await ego_crud.get_proposal(db, "p1")
        assert row["status"] == "approved"

    async def test_resolve_expected_revision_mismatch_refuses(self, db):
        """Stale revision -> refused, row untouched (optimistic-concurrency guard)."""
        await ego_crud.create_proposal(db, id="p1", action_type="t", content="c")
        ok = await ego_crud.resolve_proposal(
            db, "p1", status="approved", expected_revision=2,
        )
        assert ok is False
        row = await ego_crud.get_proposal(db, "p1")
        assert row["status"] == "pending"
        assert row["resolved_at"] is None

    async def test_batch_delivery_mapping(self, db):
        await ego_crud.set_state(
            db, key="delivery_batch:msg123", value="batch_abc",
        )
        assert await ego_crud.get_batch_for_delivery(db, "msg123") == "batch_abc"
        assert await ego_crud.get_batch_for_delivery(db, "unknown") is None


# ---------------------------------------------------------------------------
# Workflow integration tests
# ---------------------------------------------------------------------------


class TestProposalWorkflow:
    async def test_create_batch_inserts(self, workflow, db):
        batch_id, ids, _ = await workflow.create_batch(
            _sample_proposals(3), cycle_id="cycle1",
        )
        assert len(ids) == 3
        assert len(batch_id) == 16

        rows = await ego_crud.list_proposals_by_batch(db, batch_id)
        assert len(rows) == 3
        assert all(r["cycle_id"] == "cycle1" for r in rows)
        assert all(r["batch_id"] == batch_id for r in rows)

    async def test_create_batch_with_new_fields(self, workflow, db):
        props = [{
            "action_type": "investigate",
            "action_category": "system_health",
            "content": "Check backlog",
            "rationale": "Growing",
            "confidence": 0.85,
            "rank": 1,
            "execution_plan": "background CC, ~$0.30",
            "recurring": True,
        }]
        batch_id, ids, _ = await workflow.create_batch(props)
        row = (await ego_crud.list_proposals_by_batch(db, batch_id))[0]
        assert row["rank"] == 1
        assert row["execution_plan"] == "background CC, ~$0.30"
        assert row["recurring"] == 1

    async def test_create_batch_valid_goal_id(self, workflow, db):
        """Valid goal_id is stored when it matches an active goal."""
        # Create user_goals table and insert a goal
        await db.execute(TABLES["user_goals"])
        from genesis.db.crud import user_goals

        goal_id = await user_goals.create(
            db, title="Land AI role", category="career",
            priority="high", description="Find an AI eng position",
        )
        props = [{
            "action_type": "investigate",
            "content": "Research Temporal for interview",
            "confidence": 0.8,
            "goal_id": goal_id,
        }]
        batch_id, ids, _ = await workflow.create_batch(props)
        row = (await ego_crud.list_proposals_by_batch(db, batch_id))[0]
        assert row["goal_id"] == goal_id

    async def test_create_batch_invalid_goal_id_dropped(self, workflow, db):
        """Invalid/hallucinated goal_id is dropped to None."""
        await db.execute(TABLES["user_goals"])
        props = [{
            "action_type": "investigate",
            "content": "Research something",
            "confidence": 0.7,
            "goal_id": "fake-goal-that-doesnt-exist",
        }]
        batch_id, ids, _ = await workflow.create_batch(props)
        row = (await ego_crud.list_proposals_by_batch(db, batch_id))[0]
        assert row["goal_id"] is None

    async def test_create_batch_no_goal_id(self, workflow, db):
        """Proposals without goal_id default to None."""
        props = [{
            "action_type": "maintenance",
            "content": "Fix memory index",
            "confidence": 0.9,
        }]
        batch_id, ids, _ = await workflow.create_batch(props)
        row = (await ego_crud.list_proposals_by_batch(db, batch_id))[0]
        assert row["goal_id"] is None

    async def test_format_digest_html(self, workflow):
        digest = workflow.format_digest(_sample_proposals(2), "batch123")
        assert "<b>Ego</b>" in digest
        assert "batch123" in digest  # first 8 chars of batch_id
        assert "<b>1.</b>" in digest
        assert "<b>2.</b>" in digest
        assert "<b>WHAT:</b>" in digest
        assert "confidence]" in digest

    async def test_format_digest_ego_source_labels(self, workflow):
        """ego_source maps to readable labels in header."""
        props = [{"action_type": "investigate", "content": "Test", "confidence": 0.5}]
        user_digest = workflow.format_digest(props, "b1", ego_source="user_ego_cycle")
        assert "<b>User Ego</b>" in user_digest
        gen_digest = workflow.format_digest(props, "b1", ego_source="genesis_ego_cycle")
        assert "<b>Genesis Ego</b>" in gen_digest

    async def test_format_digest_escapes_html(self, workflow):
        bad = [{"action_type": "<script>", "content": "a<b>c", "confidence": 0.5}]
        digest = workflow.format_digest(bad, "batch1")
        assert "<script>" not in digest
        assert "&lt;script&gt;" in digest

    async def test_format_digest_alternatives_not_shown(self, workflow):
        """Alternatives dropped from digest — WHAT/WHY/HOW format only."""
        props = [_sample_proposals(1)[0]]  # has alternatives field
        digest = workflow.format_digest(props, "b1")
        assert "Alternatives:" not in digest

    async def test_format_digest_alternatives_hidden(self, workflow):
        props = [_sample_proposals(2)[1]]  # no alternatives key or empty
        digest = workflow.format_digest(props, "b1")
        assert "Alternatives:" not in digest

    async def test_format_digest_memory_basis_not_shown(self, workflow):
        """memory_basis dropped from digest — internal attribution only."""
        props = [{"action_type": "investigate", "content": "Test",
                  "memory_basis": "the freelance goal from March",
                  "confidence": 0.8}]
        digest = workflow.format_digest(props, "b1")
        assert "freelance goal" not in digest

    async def test_format_digest_memory_basis_hidden_when_empty(self, workflow):
        """Empty memory_basis does not render."""
        props = [{"action_type": "investigate", "content": "Test",
                  "memory_basis": "", "confidence": 0.8}]
        digest = workflow.format_digest(props, "b1")
        # Should not have an empty italic tag
        assert "<i></i>" not in digest

    async def test_format_digest_rationale_shown_as_why(self, workflow):
        """Rationale renders under WHY label."""
        props = [{"action_type": "investigate", "content": "Test",
                  "rationale": "This matters because deadlines",
                  "confidence": 0.8}]
        digest = workflow.format_digest(props, "b1")
        assert "<b>WHY:</b>" in digest
        assert "This matters because deadlines" in digest

    async def test_format_digest_execution_plan_shown_as_how(self, workflow):
        """execution_plan renders under HOW label when present."""
        props = [{"action_type": "dispatch", "content": "Run analysis",
                  "execution_plan": "Background CC session, opus model",
                  "confidence": 0.9}]
        digest = workflow.format_digest(props, "b1")
        assert "<b>HOW:</b>" in digest
        assert "Background CC session" in digest

    async def test_format_digest_no_how_when_plan_empty(self, workflow):
        """HOW section omitted when execution_plan is empty."""
        props = [{"action_type": "outreach", "content": "Notify user",
                  "confidence": 0.9}]
        digest = workflow.format_digest(props, "b1")
        assert "<b>HOW:</b>" not in digest

    async def test_format_digest_surfaces_revision_and_verified(self, workflow):
        """rev badge + 'verified as of' render when non-default (a revised /
        reaffirmed proposal); this is the PR-6b-facing surfacing."""
        props = [
            {
                "action_type": "investigate", "content": "c", "confidence": 0.9,
                "revision_num": 3, "created_at": "2026-07-01T00:00:00+00:00",
                "last_validated_at": "2026-07-05T12:00:00+00:00",
            }
        ]
        digest = workflow.format_digest(props, "b1")
        assert "[rev 3]" in digest
        assert "verified as of 2026-07-05T12:00" in digest

    async def test_format_digest_dark_when_default(self, workflow):
        """Fresh proposals (revision_num=1, last_validated_at==created_at) show
        neither badge — no noise while the reconcile revise path is dark."""
        props = [
            {
                "action_type": "investigate", "content": "c", "confidence": 0.9,
                "revision_num": 1, "created_at": "2026-07-01T00:00:00+00:00",
                "last_validated_at": "2026-07-01T00:00:00+00:00",
            }
        ]
        digest = workflow.format_digest(props, "b1")
        assert "[rev" not in digest
        assert "verified as of" not in digest

    async def test_send_digest_calls_topic_manager(
        self, workflow, db, mock_topic_manager,
    ):
        batch_id, _, _ = await workflow.create_batch(_sample_proposals(2))
        delivery = await workflow.send_digest(batch_id)
        assert delivery == "msg12345"
        mock_topic_manager.send_to_category.assert_called_once()
        call_args = mock_topic_manager.send_to_category.call_args
        assert call_args[0][0] == "ego_proposals"

    async def test_send_digest_stores_mapping(self, workflow, db):
        batch_id, _, _ = await workflow.create_batch(_sample_proposals(1))
        delivery = await workflow.send_digest(batch_id)
        assert await ego_crud.get_batch_for_delivery(db, delivery) == batch_id
        assert await ego_crud.get_state(
            db, f"batch_delivery:{batch_id}",
        ) == delivery

    async def test_send_digest_no_topic_manager(self, db):
        wf = ProposalWorkflow(db=db, topic_manager=None)
        batch_id, _, _ = await wf.create_batch(_sample_proposals(1))
        assert await wf.send_digest(batch_id) is None

    async def test_send_digest_delivers_regardless_of_quality_verdict(
        self, workflow, db, mock_topic_manager,
    ):
        """Regression: the removed LLM quality gate must not creep back.

        A proposal carrying a legacy realist_verdict='quality_hold' is still
        delivered — send_digest no longer filters on it. The Opus realist is the
        sole proposal gate; pre-removal this batch was silently dropped (None)."""
        props = _sample_proposals(2)
        for p in props:
            p["_realist_verdict"] = "quality_hold"
        batch_id, _, _ = await workflow.create_batch(props)
        delivery = await workflow.send_digest(batch_id)
        assert delivery == "msg12345"  # delivered, not dropped
        mock_topic_manager.send_to_category.assert_called_once()

    async def test_correction_stored_on_reject_with_reason(
        self, workflow, db, mock_memory_store,
    ):
        batch_id, ids, _ = await workflow.create_batch(_sample_proposals(2))
        await workflow.send_digest(batch_id)
        mock_memory_store.reset_mock()

        # Reject proposal 1 with a reason
        results = await workflow.resolve_proposals(
            batch_id, {1: ("rejected", "waste of time")},
        )
        assert results[ids[0]] == "rejected"

        # Verify correction was stored
        mock_memory_store.store.assert_called_once()
        call_kwargs = mock_memory_store.store.call_args[1]
        assert "waste of time" in call_kwargs["content"]
        assert call_kwargs["wing"] == "autonomy"
        assert call_kwargs["room"] == "ego_corrections"
        assert "ego_correction" in call_kwargs["tags"]

    async def test_no_correction_on_reject_without_reason(
        self, workflow, db, mock_memory_store,
    ):
        batch_id, ids, _ = await workflow.create_batch(_sample_proposals(1))
        mock_memory_store.reset_mock()

        await workflow.resolve_proposals(
            batch_id, {1: ("rejected", None)},
        )
        mock_memory_store.store.assert_not_called()

    async def test_no_correction_on_approve(
        self, workflow, db, mock_memory_store,
    ):
        batch_id, ids, _ = await workflow.create_batch(_sample_proposals(1))
        mock_memory_store.reset_mock()

        await workflow.resolve_proposals(
            batch_id, {1: ("approved", None)},
        )
        mock_memory_store.store.assert_not_called()

    async def test_correction_failure_does_not_block(self, db, mock_topic_manager):
        """If memory_store.store raises, proposal still gets resolved."""
        bad_store = AsyncMock()
        bad_store.store.side_effect = RuntimeError("Qdrant down")
        wf = ProposalWorkflow(
            db=db,
            topic_manager=mock_topic_manager,
            memory_store=bad_store,
        )
        batch_id, ids, _ = await wf.create_batch(_sample_proposals(1))
        results = await wf.resolve_proposals(
            batch_id, {1: ("rejected", "bad idea")},
        )
        assert results[ids[0]] == "rejected"
        row = await ego_crud.get_proposal(db, ids[0])
        assert row["status"] == "rejected"

    async def test_create_batch_stamps_revalidation(self, workflow, db):
        """create_batch stamps revalidate_at per urgency; last_validated_at=created_at."""
        from datetime import datetime

        props = [
            {"action_type": "investigate", "content": "high one", "urgency": "high"},
            {"action_type": "investigate", "content": "low one", "urgency": "low"},
            {"action_type": "investigate", "content": "default normal one"},
        ]
        batch_id, ids, _ = await workflow.create_batch(props)
        rows = {
            r["content"]: r
            for r in await ego_crud.list_proposals_by_batch(db, batch_id)
        }

        def _delta_h(row):
            c = datetime.fromisoformat(row["created_at"])
            rv = datetime.fromisoformat(row["revalidate_at"])
            return round((rv - c).total_seconds() / 3600)

        for r in rows.values():
            assert r["last_validated_at"] == r["created_at"]
            assert r["revalidate_at"] > r["created_at"]

        # EgoConfig defaults: high=48h, low=168h, normal=72h
        assert _delta_h(rows["high one"]) == 48
        assert _delta_h(rows["low one"]) == 168
        assert _delta_h(rows["default normal one"]) == 72

    async def test_create_proposal_direct_no_revalidation_stays_null(self, db):
        """Informational path (direct create_proposal) leaves revalidation NULL."""
        await ego_crud.create_proposal(
            db,
            id="info1",
            action_type="j9_regression",
            content="eval regression alert",
        )
        row = await ego_crud.get_proposal(db, "info1")
        assert row["revalidate_at"] is None
        assert row["last_validated_at"] is None

    async def test_send_digest_writes_revision_snapshot(self, workflow, db):
        """send_digest snapshots {id: revision_num} keyed by batch_id, so a
        later reply can pin the revision the user saw."""
        import json

        batch_id, ids, _ = await workflow.create_batch(_sample_proposals(2))
        await workflow.send_digest(batch_id)
        raw = await ego_crud.get_state(db, f"revision_snapshot:{batch_id}")
        assert raw is not None
        snap = json.loads(raw)
        # Fresh proposals are all revision_num=1 (no revise until PR-6b).
        assert snap == {ids[0]: 1, ids[1]: 1}

    async def test_resolve_refuses_stale_revision(self, workflow, db):
        """A row revised (revision_num bumped) after the digest is refused on
        resolve — expected_revision no longer matches — while its unchanged
        sibling resolves. The atomic optimistic-concurrency guard."""
        batch_id, ids, _ = await workflow.create_batch(_sample_proposals(2))
        await workflow.send_digest(batch_id)  # snapshot both at rev 1
        # Simulate a concurrent revise bumping proposal #1 after the digest.
        await db.execute(
            "UPDATE ego_proposals SET revision_num = 2 WHERE id = ?", (ids[0],)
        )
        await db.commit()

        results = await workflow.resolve_proposals(
            batch_id, {1: ("approved", None), 2: ("approved", None)}
        )

        # The stale one is refused (absent from results, stays pending); the
        # unchanged one resolves.
        assert ids[0] not in results
        assert results.get(ids[1]) == "approved"
        assert (await ego_crud.get_proposal(db, ids[0]))["status"] == "pending"
        assert (await ego_crud.get_proposal(db, ids[1]))["status"] == "approved"

    async def test_resolve_missing_snapshot_stays_unguarded(self, workflow, db):
        """FOOTGUN GUARD: with no snapshot (e.g. a pre-deploy digest), the
        resolve must fall back to expected_revision=None (unguarded), NOT
        default to 1 — otherwise a row at revision_num!=1 would wrongly refuse.
        A row bumped to rev 2 with no snapshot must still resolve."""
        batch_id, ids, _ = await workflow.create_batch(_sample_proposals(1))
        # No send_digest → no revision_snapshot row exists for this batch.
        await db.execute(
            "UPDATE ego_proposals SET revision_num = 2 WHERE id = ?", (ids[0],)
        )
        await db.commit()

        results = await workflow.resolve_proposals(
            batch_id, {1: ("approved", None)}
        )

        # Resolves despite revision_num=2, proving the missing snapshot mapped
        # to None (unguarded), not a hardcoded 1.
        assert results.get(ids[0]) == "approved"
        assert (await ego_crud.get_proposal(db, ids[0]))["status"] == "approved"


class TestValidateBatch:
    """Tests for structural proposal validation."""

    @pytest.fixture
    def workflow_with_db(self, db, mock_topic_manager, mock_memory_store):
        return ProposalWorkflow(
            db=db,
            topic_manager=mock_topic_manager,
            memory_store=mock_memory_store,
        )

    @pytest.mark.asyncio
    async def test_low_confidence_execute_flagged(self, workflow_with_db):
        proposals = [{"action_type": "execute", "confidence": 0.3, "content": "deploy hotfix", "rationale": "Server is degrading rapidly"}]
        issues = await workflow_with_db.validate_batch(proposals)
        assert len(issues) == 1
        assert "low confidence" in issues[0]

    @pytest.mark.asyncio
    async def test_empty_rationale_flagged(self, workflow_with_db):
        proposals = [{"action_type": "investigate", "confidence": 0.8, "content": "check logs", "rationale": ""}]
        issues = await workflow_with_db.validate_batch(proposals)
        assert len(issues) == 1
        assert "rationale" in issues[0]

    @pytest.mark.asyncio
    async def test_duplicate_content_flagged(self, workflow_with_db):
        proposals = [
            {"action_type": "investigate", "confidence": 0.8, "content": "check logs", "rationale": "Something seems off with the error rates"},
            {"action_type": "investigate", "confidence": 0.8, "content": "check logs", "rationale": "Errors are elevated across services"},
        ]
        issues = await workflow_with_db.validate_batch(proposals)
        assert any("duplicate" in i for i in issues)

    @pytest.mark.asyncio
    async def test_clean_proposal_no_issues(self, workflow_with_db):
        proposals = [{"action_type": "investigate", "confidence": 0.85, "content": "review API latency trends", "rationale": "Latency p99 has been climbing for 3 days"}]
        issues = await workflow_with_db.validate_batch(proposals)
        assert issues == []


# ---------------------------------------------------------------------------
# WS-2 P1b: ledger prediction hook fires from the create_batch call site
# ---------------------------------------------------------------------------


class TestLedgerHookWiring:
    async def test_create_batch_writes_prediction_per_proposal(self, workflow, db):
        from genesis.db.crud import ledger_predictions
        from genesis.db.schema import TABLES as _ALL_TABLES

        # the suite's shared fixture builds only the ego tables
        await db.execute(_ALL_TABLES["ledger_predictions"])

        _batch_id, ids, _ = await workflow.create_batch(
            _sample_proposals(2), cycle_id="cycle-ledger",
        )
        assert len(ids) == 2
        for pid in ids:
            (row,) = await ledger_predictions.list_by_subject(
                db, action_class="ego_proposal", subject_ref_id=pid,
            )
            assert row["metric"] == "approved_and_executes"
            assert row["domain"].startswith("ego.")
