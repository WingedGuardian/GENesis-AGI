"""Tests for feedback/harvest.py — folding existing signals into the bus."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.crud import outcome_events as oe
from genesis.feedback.harvest import (
    BACKFILL_MARKER,
    OutcomeHarvester,
    _parse_execution_suffix,
)


@pytest.fixture
async def db(tmp_path):
    """Full schema (ego_proposals, outreach_history, ego_state, outcome_events)."""
    from genesis.db.schema import create_all_tables

    path = str(tmp_path / "harvest.db")
    async with aiosqlite.connect(path) as conn:
        await create_all_tables(conn)
        # Live prod carries engagement_outcome values ('acted_on', 'acknowledged',
        # '') that violate the current CHECK — the harvester must handle data that
        # already exists. Disable CHECK enforcement so the fixture can reproduce
        # that real-world condition.
        await conn.execute("PRAGMA ignore_check_constraints = ON")
        await conn.commit()
        yield conn


def _recent(*, hours: float) -> str:
    """An ISO timestamp `hours` in the past. Relative (not hardcoded) so the
    windowed run() always sees default rows — fixed-date defaults silently
    "expire" relative to the rolling window and break this suite as time passes.
    """
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


async def _add_proposal(
    db, *, pid, status, user_response=None, confidence=0.8,
    action_type="investigate", cycle_id=None,
    created_at=None, resolved_at=None,
):
    created_at = created_at or _recent(hours=2)
    resolved_at = resolved_at or _recent(hours=1)
    await db.execute(
        "INSERT INTO ego_proposals "
        "(id, action_type, content, status, confidence, user_response, "
        " cycle_id, created_at, resolved_at) "
        "VALUES (?, ?, 'content', ?, ?, ?, ?, ?, ?)",
        (pid, action_type, status, confidence, user_response, cycle_id,
         created_at, resolved_at),
    )
    await db.commit()


async def _add_outreach(
    db, *, oid, engagement_outcome, user_response=None, category="notification",
    prediction_error=None, delivered_at=None,
):
    delivered_at = delivered_at or _recent(hours=1)
    await db.execute(
        "INSERT INTO outreach_history "
        "(id, signal_type, topic, category, salience_score, channel, "
        " message_content, engagement_outcome, user_response, prediction_error, "
        " delivered_at, created_at) "
        "VALUES (?, 'sig', 'topic', ?, 0.5, 'telegram', 'msg', ?, ?, ?, ?, ?)",
        (oid, category, engagement_outcome, user_response, prediction_error,
         delivered_at, _recent(hours=2)),
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# Suffix parser
# --------------------------------------------------------------------------- #
class TestSuffixParser:
    def test_session_prefixed_completed(self):
        assert _parse_execution_suffix("session:abc|completed:did it") == (
            "positive", 1.0, "did it",
        )

    def test_bare_completed(self):
        assert _parse_execution_suffix("|completed:done") == ("positive", 1.0, "done")

    def test_failed(self):
        polarity, value, summary = _parse_execution_suffix("sid|failed:broke")
        assert polarity == "negative" and value == 0.0 and summary == "broke"

    def test_no_suffix(self):
        assert _parse_execution_suffix("just a user reason") is None
        assert _parse_execution_suffix(None) is None
        assert _parse_execution_suffix("") is None


# --------------------------------------------------------------------------- #
# Proposal harvest
# --------------------------------------------------------------------------- #
class TestHarvestProposals:
    @pytest.mark.asyncio
    async def test_executed_with_suffix_is_t1_ground_truth(self, db):
        await _add_proposal(
            db, pid="p1", status="executed",
            user_response="session:s1|completed:shipped the fix",
            confidence=0.9, cycle_id="cyc1",
        )
        await OutcomeHarvester(db).run_backfill()
        rows = await oe.recent(db, limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["signal_type"] == "execution_outcome"
        assert row["signal_tier"] == 1
        assert row["polarity"] == "positive"
        assert row["value"] == 1.0
        assert row["domain"] == "investigate"
        assert row["stated_confidence"] == 0.9
        assert row["reason_text"] == "shipped the fix"
        assert row["metadata"] == '{"cycle_id": "cyc1"}'
        assert row["harvested_from"] == "ego_proposals"

    @pytest.mark.asyncio
    async def test_failed_is_t1_negative(self, db):
        await _add_proposal(db, pid="p2", status="failed")
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "execution_outcome"
        assert row["signal_tier"] == 1
        assert row["polarity"] == "negative"
        assert row["value"] == 0.0

    @pytest.mark.asyncio
    async def test_executed_without_suffix_is_coverage(self, db):
        await _add_proposal(db, pid="p3", status="executed", user_response="session:s3")
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "dispatch"
        assert row["signal_tier"] == 3

    @pytest.mark.asyncio
    async def test_rejected_with_reason_is_t2(self, db):
        await _add_proposal(
            db, pid="p4", status="rejected", user_response="not now, bad timing",
        )
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "user_decision"
        assert row["signal_tier"] == 2  # rationale present → informative
        assert row["polarity"] == "negative"
        assert row["reason_text"] == "not now, bad timing"

    @pytest.mark.asyncio
    async def test_tabled_and_withdrawn_are_lifecycle_t3(self, db):
        await _add_proposal(db, pid="p5", status="tabled")
        await _add_proposal(db, pid="p6", status="withdrawn")
        await OutcomeHarvester(db).run_backfill()
        by_type = await oe.count_by_signal_type(db)
        assert by_type == {"lifecycle_tabled": 1, "lifecycle_withdrawn": 1}

    @pytest.mark.asyncio
    async def test_pending_is_skipped(self, db):
        await _add_proposal(db, pid="p7", status="pending")
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 0

    @pytest.mark.asyncio
    async def test_expired_is_lifecycle_t3(self, db):
        # A timeout is NOT disapproval — coverage only.
        await _add_proposal(db, pid="pe", status="expired")
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "lifecycle_expired"
        assert row["signal_tier"] == 3
        assert row["polarity"] == "neutral"


# --------------------------------------------------------------------------- #
# Outreach harvest
# --------------------------------------------------------------------------- #
class TestHarvestOutreach:
    @pytest.mark.asyncio
    async def test_useful_reply_is_t2_positive(self, db):
        await _add_outreach(
            db, oid="o1", engagement_outcome="useful", user_response="thanks!",
            prediction_error=0.2,
        )
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == "outreach_reply"
        assert row["signal_tier"] == 2
        assert row["polarity"] == "positive"
        assert row["reason_text"] == "thanks!"
        assert row["prediction_error"] == 0.2
        assert row["source"] == "outreach"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome, exp_type, exp_polarity",
        [
            ("useful", "outreach_reply", "positive"),
            ("acted_on", "outreach_reply", "positive"),       # was mislabeled negative
            ("acknowledged", "outreach_reply", "positive"),   # was mislabeled negative
            ("not_useful", "outreach_reply", "negative"),
            ("ambivalent", "outreach_implicit", "neutral"),
            ("ignored", "outreach_implicit", "negative"),
        ],
    )
    async def test_engagement_polarity_mapping(self, db, outcome, exp_type, exp_polarity):
        await _add_outreach(db, oid=f"o-{outcome}", engagement_outcome=outcome)
        await OutcomeHarvester(db).run_backfill()
        row = (await oe.recent(db, limit=1))[0]
        assert row["signal_type"] == exp_type
        assert row["polarity"] == exp_polarity

    @pytest.mark.asyncio
    async def test_empty_string_outcome_is_skipped(self, db):
        # Live data has 15 empty-string rows — they are NOT a real signal.
        await _add_outreach(db, oid="empty", engagement_outcome="")
        await _add_outreach(db, oid="real", engagement_outcome="useful")
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 1  # only the real one


# --------------------------------------------------------------------------- #
# Idempotency / window / marker
# --------------------------------------------------------------------------- #
class TestIdempotencyAndScheduling:
    @pytest.mark.asyncio
    async def test_backfill_is_idempotent_and_guarded(self, db):
        await _add_proposal(
            db, pid="p1", status="executed",
            user_response="s|completed:x",
        )
        first = await OutcomeHarvester(db).run_backfill()
        assert first["skipped"] is False
        assert first["proposals"] == 1
        # Marker set
        assert await ego_crud.get_state(db, BACKFILL_MARKER) is not None
        # Second run is guarded → skipped, no new rows
        second = await OutcomeHarvester(db).run_backfill()
        assert second["skipped"] is True
        assert await oe.count(db) == 1

    @pytest.mark.asyncio
    async def test_backfill_marker_not_set_when_a_source_fails(self, db):
        # Reliability guard: a source exception must NOT lock the backfill gate,
        # else the historical rows are lost forever. (Reproduce by dropping a
        # source table — the startup-race / missing-table scenario.)
        await _add_proposal(
            db, pid="p1", status="executed", user_response="s|completed:x",
        )
        await db.execute("DROP TABLE outreach_history")
        await db.commit()
        result = await OutcomeHarvester(db).run_backfill()
        assert result.get("incomplete") is True
        assert result["proposals"] == 1  # per-source isolation: proposals still ran
        assert await ego_crud.get_state(db, BACKFILL_MARKER) is None  # NOT locked

    @pytest.mark.asyncio
    async def test_incremental_run_is_idempotent_on_unique_key(self, db):
        # _add_proposal defaults to a recent timestamp (see _recent), so the row
        # always falls inside run()'s default 2-day incremental window.
        await _add_proposal(
            db, pid="p1", status="executed", user_response="s|completed:x",
        )
        await OutcomeHarvester(db).run()
        await OutcomeHarvester(db).run()  # re-scan same window
        assert await oe.count(db) == 1  # unique key dedupes

    @pytest.mark.asyncio
    async def test_incremental_window_excludes_old_but_backfill_includes(self, db):
        await _add_proposal(
            db, pid="old", status="executed", user_response="s|completed:x",
            created_at="2026-05-01T00:00:00+00:00",
            resolved_at="2026-05-01T00:00:00+00:00",
        )
        # Recent window misses the old row...
        run_result = await OutcomeHarvester(db).run(window_days=2)
        assert run_result["proposals"] == 0
        assert await oe.count(db) == 0
        # ...but backfill (all history) captures it.
        await OutcomeHarvester(db).run_backfill()
        assert await oe.count(db) == 1
