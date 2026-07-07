"""Tests for build_candidates CRUD — the capability-build lane ledger."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import build_candidates


async def _make(db, id="c1", item_key="k1", verdict="build", **kw):
    return await build_candidates.create(
        db,
        id=id,
        item_key=item_key,
        item_title=kw.pop("item_title", "Test capability"),
        source_file=kw.pop("source_file", "New Genesis Capabilities.md"),
        verdict=verdict,
        **kw,
    )


class TestCreateAndGet:
    async def test_create_and_get(self, db):
        await _make(
            db,
            batch_id="b1",
            eval_path="/tmp/eval.md",
            verdict_reason="clear fit",
            confidence="high",
            build_spec='{"requirements": ["r1"]}',
        )
        row = await build_candidates.get_by_id(db, "c1")
        assert row is not None
        assert row["item_key"] == "k1"
        assert row["verdict"] == "build"
        assert row["outcome"] == "pending"
        assert row["user_decision"] is None
        assert row["confidence"] == "high"

    async def test_get_missing_returns_none(self, db):
        assert await build_candidates.get_by_id(db, "nope") is None

    async def test_open_duplicate_item_key_raises(self, db):
        await _make(db, id="c1", item_key="k1")
        with pytest.raises(aiosqlite.IntegrityError):
            await _make(db, id="c2", item_key="k1")

    async def test_get_open_by_item_key(self, db):
        await _make(db, id="c1", item_key="k1")
        row = await build_candidates.get_open_by_item_key(db, "k1")
        assert row is not None and row["id"] == "c1"
        assert await build_candidates.get_open_by_item_key(db, "k2") is None


class TestDecisionLifecycle:
    async def test_record_user_decision_closes_candidate(self, db):
        await _make(db, id="c1", item_key="k1")
        assert await build_candidates.record_user_decision(
            db, "c1", user_decision="approved"
        )
        row = await build_candidates.get_by_id(db, "c1")
        assert row["user_decision"] == "approved"
        assert row["decided_at"] is not None
        # No longer "open" — item can get a fresh candidate on re-drop.
        assert await build_candidates.get_open_by_item_key(db, "k1") is None

    async def test_invalid_decision_raises(self, db):
        await _make(db, id="c1")
        with pytest.raises(ValueError):
            await build_candidates.record_user_decision(
                db, "c1", user_decision="maybe"
            )

    async def test_decision_on_missing_row_returns_false(self, db):
        assert not await build_candidates.record_user_decision(
            db, "nope", user_decision="rejected"
        )


class TestLifecycleUpdate:
    async def test_update_fields(self, db):
        await _make(db, id="c1")
        assert await build_candidates.update(
            db,
            "c1",
            plan_path="/plans/p.md",
            approval_request_id="ar-1",
            task_id="t-abc",
            branch="task/t-abc",
            pr_url="https://example.invalid/pr/1",
            outcome="pr_opened",
            scope_gate_result='{"allowed": true}',
        )
        row = await build_candidates.get_by_id(db, "c1")
        assert row["task_id"] == "t-abc"
        assert row["outcome"] == "pr_opened"
        assert row["approval_request_id"] == "ar-1"

    async def test_invalid_outcome_raises(self, db):
        await _make(db, id="c1")
        with pytest.raises(ValueError):
            await build_candidates.update(db, "c1", outcome="shipped")

    async def test_no_fields_returns_false(self, db):
        await _make(db, id="c1")
        assert not await build_candidates.update(db, "c1")


class TestQueries:
    async def test_list_open_excludes_decided(self, db):
        await _make(db, id="c1", item_key="k1")
        await _make(db, id="c2", item_key="k2", verdict="dont_build")
        await build_candidates.record_user_decision(
            db, "c2", user_decision="rejected"
        )
        open_rows = await build_candidates.list_open(db)
        assert [r["id"] for r in open_rows] == ["c1"]

    async def test_list_recent_limit(self, db):
        for i in range(3):
            await _make(db, id=f"c{i}", item_key=f"k{i}")
        rows = await build_candidates.list_recent(db, limit=2)
        assert len(rows) == 2

    async def test_list_by_outcome(self, db):
        await _make(db, id="c1", item_key="k1")  # outcome defaults to 'pending'
        await _make(db, id="c2", item_key="k2")
        await build_candidates.update(db, "c2", outcome="submitted")
        await _make(db, id="c3", item_key="k3")
        await build_candidates.update(db, "c3", outcome="submitted")
        submitted = await build_candidates.list_by_outcome(db, "submitted")
        assert {r["id"] for r in submitted} == {"c2", "c3"}
        assert [r["id"] for r in submitted] == ["c2", "c3"]  # oldest first

    async def test_list_by_outcome_rejects_bad_value(self, db):
        with pytest.raises(ValueError):
            await build_candidates.list_by_outcome(db, "shipped")

    async def test_list_by_verdict(self, db):
        await _make(db, id="c1", item_key="k1", verdict="build")
        await _make(db, id="c2", item_key="k2", verdict="dont_build",
                    verdict_reason="duplicates existing capability")
        await _make(db, id="c3", item_key="k3", verdict="dont_build",
                    verdict_reason="brain-not-body scope")
        rows = await build_candidates.list_by_verdict(db, "dont_build")
        assert {r["id"] for r in rows} == {"c2", "c3"}
        assert all(r["verdict"] == "dont_build" for r in rows)

    async def test_list_by_verdict_limit_and_bad_value(self, db):
        for i in range(3):
            await _make(db, id=f"c{i}", item_key=f"k{i}", verdict="build")
        assert len(await build_candidates.list_by_verdict(db, "build", limit=2)) == 2
        with pytest.raises(ValueError):
            await build_candidates.list_by_verdict(db, "ship_it")

    async def test_verdict_decision_counts(self, db):
        # build: 1 approved, 1 open; dont_build: 1 (never decided)
        await _make(db, id="c1", item_key="k1", verdict="build")
        await build_candidates.record_user_decision(db, "c1", user_decision="approved")
        await _make(db, id="c2", item_key="k2", verdict="build")  # open
        await _make(db, id="c3", item_key="k3", verdict="dont_build")
        counts = await build_candidates.verdict_decision_counts(db)
        as_set = {(r["verdict"], r["user_decision"], r["count"]) for r in counts}
        assert ("build", "approved", 1) in as_set
        assert ("build", None, 1) in as_set
        assert ("dont_build", None, 1) in as_set

    async def test_get_by_approval_request_and_task(self, db):
        await _make(db, id="c1")
        await build_candidates.update(
            db, "c1", approval_request_id="ar-9", task_id="t-9"
        )
        assert (await build_candidates.get_by_approval_request(db, "ar-9"))["id"] == "c1"
        assert (await build_candidates.get_by_task(db, "t-9"))["id"] == "c1"

    async def test_create_persists_approval_request_id(self, db):
        await _make(db, id="c1", approval_request_id="ar-42")
        row = await build_candidates.get_by_id(db, "c1")
        assert row["approval_request_id"] == "ar-42"

    async def test_get_any_by_item_key_finds_decided(self, db):
        # Permanent dedup: a DECIDED row (not "open") is still found by
        # get_any_by_item_key so a rescan never re-cards a built item.
        await _make(db, id="c1", item_key="k1")
        await build_candidates.record_user_decision(
            db, "c1", user_decision="approved"
        )
        assert await build_candidates.get_open_by_item_key(db, "k1") is None
        found = await build_candidates.get_any_by_item_key(db, "k1")
        assert found is not None and found["id"] == "c1"
        assert await build_candidates.get_any_by_item_key(db, "nope") is None
