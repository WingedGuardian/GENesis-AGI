"""Tests for the J-9 eval batch executor (J9EvalBatchExecutor).

Covers the resumability/concurrency hardening: checkpoint-based dedup so a
reaper-retried run resumes instead of restarting, and the internal deadline
that returns a graceful partial instead of tripping the 2h surplus reaper.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from genesis.db.crud import j9_eval
from genesis.eval.j9_batch import J9EvalBatchExecutor


class _FakeRouter:
    """Minimal router: records call count, returns a fixed relevance verdict."""

    def __init__(self, relevance: float = 0.9) -> None:
        self.calls = 0
        self.chain_offsets: list[int] = []
        self._relevance = relevance

    async def route_call(self, call_site_id, messages, *, chain_offset=0, **kwargs):
        self.calls += 1
        self.chain_offsets.append(chain_offset)
        return SimpleNamespace(
            success=True,
            provider_used="fake",
            model_id="fake-judge",
            content=json.dumps({"relevance": self._relevance, "rationale": "ok"}),
            error=None,
        )


def _task():
    return SimpleNamespace(task_type="j9_eval_batch")


async def _seed_recall(db, *, query, memory_ids, session_id=None):
    """Insert a recall_fired event plus memory_fts content for each memory."""
    for mid in memory_ids:
        await db.execute(
            "INSERT INTO memory_fts (memory_id, content) VALUES (?, ?)",
            (mid, f"content for {mid}"),
        )
    await db.commit()
    return await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_fired",
        session_id=session_id,
        metrics={"query": query, "memory_ids": memory_ids},
    )


async def _relevance_pairs(db) -> set[tuple[str, str]]:
    events = await j9_eval.get_events(
        db, dimension="memory", event_type="recall_relevance", limit=1000,
    )
    return {
        (e["metrics"]["recall_event_id"], e["metrics"]["memory_id"])
        for e in events
    }


async def test_no_recall_events_returns_success(db):
    ex = J9EvalBatchExecutor(db=db, router=_FakeRouter())
    result = await ex.execute(_task())
    assert result.success
    assert "no recall events" in result.content


async def test_judges_all_memories_happy_path(db):
    eid = await _seed_recall(db, query="what is X", memory_ids=["m1", "m2", "m3"])
    router = _FakeRouter()
    ex = J9EvalBatchExecutor(db=db, router=router)

    result = await ex.execute(_task())

    assert result.success
    assert router.calls == 3  # one judge call per memory
    assert await _relevance_pairs(db) == {(eid, "m1"), (eid, "m2"), (eid, "m3")}


async def test_checkpoint_skips_already_judged(db):
    eid = await _seed_recall(db, query="q", memory_ids=["m1", "m2"])
    # Pre-existing judgment for (eid, m1) — simulates a prior partial/retry.
    await j9_eval.insert_event(
        db,
        dimension="memory",
        event_type="recall_relevance",
        subject_id="m1",
        metrics={"recall_event_id": eid, "memory_id": "m1", "relevance": 0.5},
    )
    router = _FakeRouter()
    ex = J9EvalBatchExecutor(db=db, router=router)

    result = await ex.execute(_task())

    assert result.success
    assert router.calls == 1  # only m2 judged; m1 skipped via checkpoint
    pairs = await _relevance_pairs(db)
    assert (eid, "m1") in pairs and (eid, "m2") in pairs
    assert len(pairs) == 2  # no duplicate for m1


async def test_deadline_defers_remaining(db, monkeypatch):
    await _seed_recall(db, query="q", memory_ids=["m1", "m2"])
    router = _FakeRouter()
    ex = J9EvalBatchExecutor(db=db, router=router)

    # First monotonic() call sets the deadline; all later checks read past it.
    state = {"n": 0}

    def fake_monotonic():
        state["n"] += 1
        return 0.0 if state["n"] == 1 else 1e9

    monkeypatch.setattr("genesis.eval.j9_batch.time.monotonic", fake_monotonic)

    result = await ex.execute(_task())

    assert result.success  # graceful partial, NOT a failure
    assert "PARTIAL" in result.content
    assert router.calls == 0  # everything deferred to next run
    assert len(await _relevance_pairs(db)) == 0
