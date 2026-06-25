"""Tests for the outcome_events table, migration 0025, and its CRUD.

Covers the fresh-install path (create_all_tables / _tables.py), the versioned
migration (up/down/idempotency), and CRUD semantics — including the regression
guard for the architect's CRITICAL finding: a T2 user_decision and a T1
execution_outcome on the SAME proposal must both persist (distinct signal_type),
never colliding on the unique key and silently dropping ground truth.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import outcome_events as oe

# Migration module name starts with a digit — must import via importlib.
MIGRATION = importlib.import_module("genesis.db.migrations.0025_outcome_events")


@pytest.fixture
async def db(tmp_path):
    """Fresh DB with outcome_events created via the real migration up()."""
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        await MIGRATION.up(conn)  # up() must not commit — runner owns the txn
        await conn.commit()
        yield conn


# --------------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------------- #
class TestSchema:
    @pytest.mark.asyncio
    async def test_table_and_indexes_exist(self, db):
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outcome_events'"
        )
        assert await cur.fetchone() is not None

        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_outcome_events_%'"
        )
        idx = {r[0] for r in await cur.fetchall()}
        assert "idx_outcome_events_domain" in idx
        assert "idx_outcome_events_tier" in idx
        assert "idx_outcome_events_calibration" in idx  # partial index

    @pytest.mark.asyncio
    async def test_up_is_idempotent(self, tmp_path):
        path = str(tmp_path / "idem.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.up(conn)  # IF NOT EXISTS → must not raise
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='outcome_events'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_down_drops_table(self, tmp_path):
        path = str(tmp_path / "down.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await conn.commit()
            await MIGRATION.down(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='outcome_events'"
            )
            assert await cur.fetchone() is None

    @pytest.mark.asyncio
    async def test_fresh_install_path_creates_table(self, tmp_path):
        """create_all_tables (the _tables.py registration) must include it."""
        from genesis.db.schema import create_all_tables

        path = str(tmp_path / "fresh.db")
        async with aiosqlite.connect(path) as conn:
            await create_all_tables(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='outcome_events'"
            )
            assert await cur.fetchone() is not None

    @pytest.mark.asyncio
    async def test_check_constraint_rejects_bad_tier_at_db_level(self, db):
        """A plain INSERT (not OR IGNORE) with a bad tier must hit the CHECK."""
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO outcome_events "
                "(id, source, ref_type, ref_id, signal_type, signal_tier, occurred_at) "
                "VALUES ('x','ego','proposal','p','execution_outcome', 9, '2026-01-01')"
            )


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
class TestRecord:
    @pytest.mark.asyncio
    async def test_record_returns_id_and_persists(self, db):
        eid = await oe.record(
            db, source="ego", ref_type="proposal", ref_id="p1",
            signal_type="execution_outcome", signal_tier=1, domain="investigate",
            polarity="positive", value=1.0, stated_confidence=0.8,
            metadata={"session": "s1"}, harvested_from="ego_proposals",
        )
        assert isinstance(eid, str) and len(eid) == 16
        assert await oe.count(db) == 1
        rows = await oe.recent(db, limit=5)
        assert rows[0]["ref_id"] == "p1"
        assert rows[0]["signal_tier"] == 1
        assert rows[0]["metadata"] == '{"session": "s1"}'  # serialized

    @pytest.mark.asyncio
    async def test_record_idempotent_on_unique_key(self, db):
        key = dict(
            source="ego", ref_type="proposal", ref_id="p2",
            signal_type="user_decision", signal_tier=2,
        )
        first = await oe.record(db, **key, reason="timing")
        second = await oe.record(db, **key, reason="safety")  # same unique key
        assert first is not None
        assert second is None  # OR IGNORE → not inserted
        assert await oe.count(db) == 1

    @pytest.mark.asyncio
    async def test_distinct_signal_types_coexist_on_same_ref(self, db):
        """REGRESSION (architect Finding 1): T2 user_decision + T1 execution_outcome
        on ONE proposal must both survive — ground truth never dropped."""
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="p3",
            signal_type="user_decision", signal_tier=2, polarity="positive",
        )
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="p3",
            signal_type="execution_outcome", signal_tier=1,
            polarity="positive", value=1.0,
        )
        assert await oe.count(db) == 2
        assert await oe.count_by_signal_type(db) == {
            "user_decision": 1, "execution_outcome": 1,
        }

    @pytest.mark.asyncio
    async def test_exists(self, db):
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="p4",
            signal_type="execution_outcome", signal_tier=1,
        )
        assert await oe.exists(
            db, source="ego", ref_type="proposal", ref_id="p4",
            signal_type="execution_outcome",
        ) is True
        assert await oe.exists(
            db, source="ego", ref_type="proposal", ref_id="p4",
            signal_type="user_decision",
        ) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(signal_tier=4),                      # bad tier
            dict(signal_tier=1, signal_class="bogus"),  # bad class
            dict(signal_tier=1, polarity="sideways"),   # bad polarity
            dict(signal_tier=1, source=""),             # empty key field
        ],
    )
    async def test_record_validates(self, db, kwargs):
        base = dict(
            source="ego", ref_type="proposal", ref_id="pX",
            signal_type="execution_outcome",
        )
        base.update(kwargs)
        with pytest.raises(ValueError):
            await oe.record(db, **base)


class TestAggregates:
    @pytest.mark.asyncio
    async def test_aggregate_by_domain(self, db):
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="a1",
            signal_type="execution_outcome", signal_tier=1, domain="investigate",
            polarity="positive", value=1.0, stated_confidence=0.9,
        )
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="a2",
            signal_type="execution_outcome", signal_tier=1, domain="investigate",
            polarity="negative", value=0.0, stated_confidence=0.6,
        )
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="a3",
            signal_type="execution_outcome", signal_tier=1, domain="outreach",
            polarity="positive", value=1.0,
        )
        aggs = await oe.aggregate_by_domain(db, days=30)
        inv = next(a for a in aggs if a["domain"] == "investigate")
        assert inv["n"] == 2
        assert inv["positive"] == 1
        assert inv["negative"] == 1
        assert abs(inv["avg_value"] - 0.5) < 1e-9

    @pytest.mark.asyncio
    async def test_aggregate_tier_filter(self, db):
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="t1",
            signal_type="execution_outcome", signal_tier=1, domain="d",
        )
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="t2",
            signal_type="user_decision", signal_tier=3, domain="d",
        )
        aggs = await oe.aggregate_by_domain(db, tier=1)
        d = next(a for a in aggs if a["domain"] == "d")
        assert d["n"] == 1

    @pytest.mark.asyncio
    async def test_count_by_tier(self, db):
        # Two T1, one T3 — tier cannot be derived from signal_type alone, so
        # this needs its own GROUP BY (the soak instrument relies on it).
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="ct1",
            signal_type="execution_outcome", signal_tier=1, domain="d",
        )
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="ct2",
            signal_type="execution_outcome", signal_tier=1, domain="e",
        )
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="ct3",
            signal_type="dispatch", signal_tier=3, domain="d",
        )
        assert await oe.count_by_tier(db) == {1: 2, 3: 1}

    @pytest.mark.asyncio
    async def test_count_by_tier_empty(self, db):
        assert await oe.count_by_tier(db) == {}

    @pytest.mark.asyncio
    async def test_aggregate_all_time_window(self, db):
        # A row outside the 30-day window is excluded by default but included
        # when days=None — the all-time view the soak instrument uses so the
        # per-domain breakdown reconciles with the lifetime tier counts.
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="old",
            signal_type="execution_outcome", signal_tier=1, domain="d",
            value=1.0, occurred_at="2020-01-01T00:00:00+00:00",
        )
        assert await oe.aggregate_by_domain(db, tier=1, days=30) == []
        all_time = await oe.aggregate_by_domain(db, tier=1, days=None)
        d = next(a for a in all_time if a["domain"] == "d")
        assert d["n"] == 1

    @pytest.mark.asyncio
    async def test_calibration_by_domain_requires_confidence_and_value(self, db):
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="c1",
            signal_type="execution_outcome", signal_tier=1, domain="investigate",
            stated_confidence=0.8, value=1.0,
        )
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="c2",
            signal_type="execution_outcome", signal_tier=1, domain="investigate",
            stated_confidence=0.5, value=0.0,
        )
        # Excluded: has confidence but no graded value.
        await oe.record(
            db, source="ego", ref_type="proposal", ref_id="c3",
            signal_type="execution_outcome", signal_tier=1, domain="investigate",
            stated_confidence=0.7,
        )
        rows = await oe.calibration_by_domain(db, tier=1)
        assert len(rows) == 2
        assert all(
            r["stated_confidence"] is not None and r["value"] is not None
            for r in rows
        )
