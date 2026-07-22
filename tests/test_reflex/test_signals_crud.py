"""Tests for reflex_signals CRUD — fingerprint upsert, reopen, transitions.

All timestamps are injected (no wall clock). ISO-UTC strings compare
lexicographically, which the mute-window check relies on.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import reflex_signals as crud

M70 = importlib.import_module("genesis.db.migrations.0070_reflex_arc")

T0 = "2026-07-21T00:00:00+00:00"
T1 = "2026-07-21T01:00:00+00:00"
T2 = "2026-07-21T02:00:00+00:00"
FUTURE = "2026-08-01T00:00:00+00:00"


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        await M70.up(conn)
        await conn.commit()
        yield conn


async def _upsert(db, *, fingerprint="fp00000000000001", now=T0, **overrides):
    kwargs = {
        "fingerprint": fingerprint,
        "class_key": "KeyErrorxmemory",
        "task_name": "mem-sync",
        "subsystem": "memory",
        "error_type": "KeyError",
        "error_message": "KeyError: 'x'",
        "traceback_tail": "memory/sync.py:_apply>memory/store.py:get",
        "now": now,
    }
    kwargs.update(overrides)
    return await crud.upsert_occurrence(db, **kwargs)


class TestUpsert:
    async def test_insert_creates_new_row(self, db):
        row = await _upsert(db)
        assert row["status"] == "new"
        assert row["occurrence_count"] == 1
        assert row["first_seen_at"] == T0
        assert row["last_seen_at"] == T0
        assert row["fingerprint"] == "fp00000000000001"
        assert row["class_key"] == "KeyErrorxmemory"

    async def test_conflict_increments_and_advances_last_seen(self, db):
        await _upsert(db, now=T0)
        row = await _upsert(db, now=T1, error_message="KeyError: 'y'")
        assert row["occurrence_count"] == 2
        assert row["first_seen_at"] == T0  # preserved
        assert row["last_seen_at"] == T1
        assert row["last_error_message"] == "KeyError: 'y'"  # latest wins

    async def test_burst_of_n_is_one_row(self, db):
        for i in range(10):
            await _upsert(db, now=f"2026-07-21T00:00:{i:02d}+00:00")
        cur = await db.execute("SELECT COUNT(*) FROM reflex_signals")
        assert (await cur.fetchone())[0] == 1
        row = await crud.get_by_fingerprint(db, "fp00000000000001")
        assert row["occurrence_count"] == 10

    async def test_distinct_fingerprints_distinct_rows(self, db):
        await _upsert(db, fingerprint="fp00000000000001")
        await _upsert(db, fingerprint="fp00000000000002")
        cur = await db.execute("SELECT COUNT(*) FROM reflex_signals")
        assert (await cur.fetchone())[0] == 2

    async def test_conflict_does_not_touch_status(self, db):
        await _upsert(db, now=T0)
        row = await crud.get_by_fingerprint(db, "fp00000000000001")
        assert await crud.set_status(
            db, signal_id=row["id"], expected_from="new", to="carded_diagnose", now=T1
        )
        row = await _upsert(db, now=T2)
        assert row["status"] == "carded_diagnose"
        assert row["occurrence_count"] == 2


_TERMINAL = [
    "merged",
    "resolved",
    "dismissed_notbug",
    "dismissed_wontfix",
    "card_expired",
    "diagnose_failed",
    "fix_failed",
]


class TestReopen:
    @pytest.mark.parametrize("terminal", _TERMINAL)
    async def test_reopens_from_each_terminal_status(self, db, terminal):
        row = await _upsert(db, now=T0)
        await crud.set_status(db, signal_id=row["id"], expected_from="new", to=terminal, now=T1)
        assert await crud.maybe_reopen(db, fingerprint="fp00000000000001", now=T2) is True
        row = await crud.get_by_fingerprint(db, "fp00000000000001")
        assert row["status"] == "new"
        assert row["reopen_count"] == 1
        assert row["reopened_at"] == T2
        assert row["diagnose_request_id"] is None
        assert row["fix_request_id"] is None
        assert row["task_id"] is None

    async def test_no_reopen_from_active_status(self, db):
        row = await _upsert(db, now=T0)
        await crud.set_status(db, signal_id=row["id"], expected_from="new", to="diagnosing", now=T1)
        assert await crud.maybe_reopen(db, fingerprint="fp00000000000001", now=T2) is False
        row = await crud.get_by_fingerprint(db, "fp00000000000001")
        assert row["status"] == "diagnosing"
        assert row["reopen_count"] == 0

    async def test_muted_until_blocks_reopen(self, db):
        row = await _upsert(db, now=T0)
        await crud.set_status(
            db, signal_id=row["id"], expected_from="new", to="dismissed_wontfix", now=T1
        )
        await db.execute(
            "UPDATE reflex_signals SET muted_until = ? WHERE id = ?", (FUTURE, row["id"])
        )
        await db.commit()
        assert await crud.maybe_reopen(db, fingerprint="fp00000000000001", now=T2) is False
        row = await crud.get_by_fingerprint(db, "fp00000000000001")
        assert row["status"] == "dismissed_wontfix"

    async def test_expired_mute_allows_reopen(self, db):
        row = await _upsert(db, now=T0)
        await crud.set_status(
            db, signal_id=row["id"], expected_from="new", to="dismissed_wontfix", now=T0
        )
        await db.execute("UPDATE reflex_signals SET muted_until = ? WHERE id = ?", (T1, row["id"]))
        await db.commit()
        assert await crud.maybe_reopen(db, fingerprint="fp00000000000001", now=T2) is True

    async def test_missing_fingerprint_returns_false(self, db):
        assert await crud.maybe_reopen(db, fingerprint="nope", now=T0) is False


class TestSetStatus:
    async def test_guarded_transition_succeeds(self, db):
        row = await _upsert(db)
        assert (
            await crud.set_status(
                db, signal_id=row["id"], expected_from="new", to="carded_diagnose", now=T1
            )
            is True
        )
        row = await crud.get_by_fingerprint(db, "fp00000000000001")
        assert row["status"] == "carded_diagnose"
        assert row["updated_at"] == T1

    async def test_wrong_expected_from_is_noop(self, db):
        row = await _upsert(db)
        assert (
            await crud.set_status(
                db, signal_id=row["id"], expected_from="diagnosing", to="diagnosed", now=T1
            )
            is False
        )
        row = await crud.get_by_fingerprint(db, "fp00000000000001")
        assert row["status"] == "new"


class TestListByStatus:
    async def test_lists_matching_only(self, db):
        await _upsert(db, fingerprint="fp00000000000001")
        await _upsert(db, fingerprint="fp00000000000002")
        row2 = await crud.get_by_fingerprint(db, "fp00000000000002")
        await crud.set_status(
            db, signal_id=row2["id"], expected_from="new", to="diagnosing", now=T1
        )
        rows = await crud.list_by_status(db, "new")
        assert [r["fingerprint"] for r in rows] == ["fp00000000000001"]


class TestAggregates:
    """PR1.5 observability aggregates — shared by the MCP tool and the snapshot."""

    async def test_count_by_status(self, db):
        await _upsert(db, fingerprint="fp00000000000001")
        await _upsert(db, fingerprint="fp00000000000002")
        row2 = await crud.get_by_fingerprint(db, "fp00000000000002")
        await crud.set_status(
            db, signal_id=row2["id"], expected_from="new", to="diagnosing", now=T1
        )
        counts = await crud.count_by_status(db)
        assert counts == {"new": 1, "diagnosing": 1}

    async def test_count_by_status_empty(self, db):
        assert await crud.count_by_status(db) == {}

    async def test_top_class_keys_orders_by_signal_count_then_occurrences(self, db):
        # 2 distinct signals in KeyErrorxmemory; 1 signal (3 occurrences) in ValueErrorxrouting
        await _upsert(db, fingerprint="fp00000000000001")
        await _upsert(db, fingerprint="fp00000000000002")
        for now in (T0, T1, T2):
            await _upsert(
                db, fingerprint="fp00000000000003", class_key="ValueErrorxrouting", now=now
            )
        top = await crud.top_class_keys(db, limit=8)
        assert top[0] == {"class_key": "KeyErrorxmemory", "signals": 2, "occurrences": 2}
        assert top[1] == {"class_key": "ValueErrorxrouting", "signals": 1, "occurrences": 3}

    async def test_top_class_keys_respects_limit(self, db):
        await _upsert(db, fingerprint="fp00000000000001", class_key="A")
        await _upsert(db, fingerprint="fp00000000000002", class_key="B")
        assert len(await crud.top_class_keys(db, limit=1)) == 1

    async def test_list_recent_orders_by_last_seen_desc(self, db):
        await _upsert(db, fingerprint="fp00000000000001", now=T0)
        await _upsert(db, fingerprint="fp00000000000002", now=T2)
        await _upsert(db, fingerprint="fp00000000000003", now=T1)
        rows = await crud.list_recent(db, limit=2)
        assert [r["fingerprint"] for r in rows] == [
            "fp00000000000002",
            "fp00000000000003",
        ]
