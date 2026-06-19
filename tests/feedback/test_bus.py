"""Tests for feedback/bus.py — the Outcome Bus write path + signal taxonomy."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import outcome_events as oe
from genesis.feedback import bus
from genesis.feedback.bus import SignalType, default_tier, record_outcome

MIGRATION = importlib.import_module("genesis.db.migrations.0025_outcome_events")


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "bus.db")
    async with aiosqlite.connect(db_path) as conn:
        await MIGRATION.up(conn)
        await conn.commit()
        yield conn


class TestTaxonomy:
    def test_tier1_ground_truth(self):
        assert default_tier(SignalType.EXECUTION_OUTCOME) == 1
        assert default_tier(SignalType.VERIFICATION_FAILED) == 1

    def test_user_decision_tier_depends_on_rationale(self):
        assert default_tier(SignalType.USER_DECISION) == 3
        assert default_tier(SignalType.USER_DECISION, has_rationale=True) == 2

    def test_informative_and_coverage(self):
        assert default_tier(SignalType.OUTREACH_REPLY) == 2
        assert default_tier(SignalType.TRIAGE_CORRECTION) == 2
        assert default_tier(SignalType.DISPATCH) == 3
        assert default_tier(SignalType.OUTREACH_IMPLICIT) == 3
        assert default_tier(SignalType.LIFECYCLE_EXPIRED) == 3

    def test_unknown_defaults_to_coverage(self):
        assert default_tier("something_new") == 3


class TestRecordOutcome:
    @pytest.mark.asyncio
    async def test_records_and_derives_tier(self, db):
        eid = await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="p1",
            signal_type=SignalType.EXECUTION_OUTCOME, polarity="positive", value=1.0,
        )
        assert eid is not None
        rows = await oe.recent(db, limit=1)
        assert rows[0]["signal_tier"] == 1  # derived from taxonomy

    @pytest.mark.asyncio
    async def test_user_decision_rationale_upgrades_tier(self, db):
        await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="with_reason",
            signal_type=SignalType.USER_DECISION, reason="timing", reason_text="not now",
        )
        await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="bare",
            signal_type=SignalType.USER_DECISION,
        )
        rows = {r["ref_id"]: r for r in await oe.recent(db, limit=10)}
        assert rows["with_reason"]["signal_tier"] == 2
        assert rows["bare"]["signal_tier"] == 3

    @pytest.mark.asyncio
    async def test_explicit_tier_overrides_default(self, db):
        await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="p2",
            signal_type=SignalType.DISPATCH, signal_tier=1,
        )
        rows = await oe.recent(db, limit=1)
        assert rows[0]["signal_tier"] == 1

    @pytest.mark.asyncio
    async def test_never_raises_on_bad_input(self, db):
        # Invalid polarity would make the strict CRUD raise ValueError; the bus
        # must swallow it and return None — production paths can't break.
        result = await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="bad",
            signal_type=SignalType.EXECUTION_OUTCOME, polarity="sideways",
        )
        assert result is None
        assert await oe.count(db) == 0

    @pytest.mark.asyncio
    async def test_idempotent(self, db):
        first = await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="dup",
            signal_type=SignalType.EXECUTION_OUTCOME,
        )
        second = await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="dup",
            signal_type=SignalType.EXECUTION_OUTCOME,
        )
        assert first is not None
        assert second is None
        assert await oe.count(db) == 1

    @pytest.mark.asyncio
    async def test_unknown_signal_type_still_records_at_t3(self, db):
        eid = await record_outcome(
            db, source="ego", ref_type="proposal", ref_id="novel",
            signal_type="novel_signal",
        )
        assert eid is not None
        rows = await oe.recent(db, limit=1)
        assert rows[0]["signal_tier"] == 3

    @pytest.mark.asyncio
    async def test_known_vocabulary_is_collision_safe(self):
        # All vocabulary values must be distinct (the unique-key guarantee).
        values = [
            getattr(SignalType, n) for n in dir(SignalType) if not n.startswith("_")
        ]
        assert len(values) == len(set(values))
        assert set(values) == set(bus.KNOWN_SIGNAL_TYPES)
