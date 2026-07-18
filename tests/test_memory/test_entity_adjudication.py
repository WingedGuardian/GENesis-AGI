"""Entity adjudication drainer — merge-vs-distinct decisions over fuzzy pairs.

Uses the full in-memory ``db`` fixture and a scripted router (no live LLM).
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.crud import deferred_work as dw_crud
from genesis.db.crud import entities as entities_crud
from genesis.db.crud import entity_adjudications as adj_crud
from genesis.memory import entity_adjudication as adj

# ── fixtures/helpers ─────────────────────────────────────────────────────────


def _router(verdict_by_callsite: dict[str, str]) -> MagicMock:
    """Router whose route_call returns a scripted verdict per call_site_id."""
    router = MagicMock()

    async def _call(call_site_id, messages, **kwargs):
        v = verdict_by_callsite.get(call_site_id, "distinct")
        return MagicMock(
            success=True,
            content=json.dumps({"verdict": v, "reasoning": "test"}),
            provider_used=f"prov-{call_site_id}",
            error=None,
        )

    router.route_call = AsyncMock(side_effect=_call)
    return router


async def _mk_entity(db, name, norm, etype="concept", summary=None):
    return await entities_crud.create_entity(
        db, name=name, norm_name=norm, entity_type=etype, summary=summary
    )


async def _enqueue(db, eid, similar):
    item_id = str(uuid.uuid4())
    await dw_crud.create(
        db,
        id=item_id,
        work_type=adj.WORK_TYPE,
        priority=60,
        payload_json=json.dumps({"entity_id": eid, "similar_entity_id": similar}),
        deferred_at="2026-07-17T00:00:00+00:00",
        deferred_reason="fuzzy",
        created_at="2026-07-17T00:00:00+00:00",
        call_site_id="entity_adjudication",
    )
    return item_id


async def _status(db, eid):
    cur = await db.execute("SELECT status FROM entities WHERE entity_id = ?", (eid,))
    return (await cur.fetchone())[0]


# ── digit guard ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("pr #989", "pr #990", True),
        ("2026-07-16", "2026-07-17", True),
        ("system", "systemd", False),
        ("pr-1", "pr-1a", False),
        ("neural monitor", "neural-monitor", False),
        ("same", "same", False),  # identical → not a "difference"
    ],
)
def test_digit_only_difference(a, b, expected):
    assert adj.digit_only_difference(a, b) is expected


# ── adjudicate_pair ──────────────────────────────────────────────────────────


def test_prompts_carry_cosmetic_vs_semantic_guidance():
    """Regression guard for the live-E2E finding: without the cosmetic-vs-semantic
    distinction, the challenge over-fires and merges nothing (formatting variants
    like 'neural monitor'/'neural-monitor' get wrongly kept distinct). Both
    prompts must keep that guidance."""
    for prompt in (adj._ADJUDICATION_PROMPT, adj._CHALLENGE_PROMPT):
        low = prompt.lower()
        assert "cosmetic" in low or "formatting" in low
        assert "semantic" in low


@pytest.mark.asyncio
async def test_adjudicate_merge_requires_both_models():
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "merge"})
    out = await adj.adjudicate_pair(router, "A", "B")
    assert out["verdict"] == "merge"
    assert "+" in out["provider"]  # both providers recorded


@pytest.mark.asyncio
async def test_adjudicate_challenge_overrides_to_distinct():
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "distinct"})
    out = await adj.adjudicate_pair(router, "A", "B")
    assert out["verdict"] == "distinct"


@pytest.mark.asyncio
async def test_adjudicate_primary_distinct_skips_challenge():
    router = _router({"entity_adjudication": "distinct"})
    out = await adj.adjudicate_pair(router, "A", "B")
    assert out["verdict"] == "distinct"
    # challenge call site never invoked
    called_sites = [c.args[0] for c in router.route_call.call_args_list]
    assert "entity_adjudication_challenge" not in called_sites


@pytest.mark.asyncio
async def test_adjudicate_primary_error_is_retryable():
    router = MagicMock()
    router.route_call = AsyncMock(return_value=MagicMock(success=False, error="boom"))
    out = await adj.adjudicate_pair(router, "A", "B")
    assert out["verdict"] == "distinct" and out["retryable"] is True


@pytest.mark.asyncio
async def test_adjudicate_challenge_dissent_is_not_retryable():
    # Real dissent (challenge genuinely says distinct) is a recordable verdict,
    # NOT a retry.
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "distinct"})
    out = await adj.adjudicate_pair(router, "A", "B")
    assert out["verdict"] == "distinct" and out["retryable"] is False


@pytest.mark.asyncio
async def test_drain_requeues_on_llm_error_without_recording(db):
    a = await _mk_entity(db, "flaky one", "flaky one")
    b = await _mk_entity(db, "flaky onee", "flaky onee")
    item_id = await _enqueue(db, a, b)
    router = MagicMock()
    router.route_call = AsyncMock(return_value=MagicMock(success=False, error="all providers down"))

    counts = await adj.run_adjudication_drain(db, router, mode="propose_only", budget=10)

    assert counts["retried"] == 1 and counts["judged"] == 0
    # No verdict recorded — a transient outage must not burn the pair.
    assert await adj_crud.get_by_pair(db, a, b) is None
    # Row is back to pending (with attempts incremented) for the next run.
    cur = await db.execute(
        "SELECT status, attempts FROM deferred_work_queue WHERE id=?", (item_id,)
    )
    status, attempts = await cur.fetchone()
    assert status == "pending" and attempts == 1


# ── drain: propose_only ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_only_records_but_never_mutates(db):
    a = await _mk_entity(db, "neural monitor", "neural monitor")
    b = await _mk_entity(db, "neural-monitor", "neural-monitor")
    await _enqueue(db, a, b)
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "merge"})

    counts = await adj.run_adjudication_drain(db, router, mode="propose_only", budget=10)

    assert counts["proposed"] == 1 and counts["merged"] == 0
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["verdict"] == "proposed_merge"
    # HARD invariant: no entity mutated in shadow mode.
    assert await _status(db, a) == "active"
    assert await _status(db, b) == "active"


@pytest.mark.asyncio
async def test_digit_guard_skips_llm(db):
    a = await _mk_entity(db, "pr #989", "pr #989")
    b = await _mk_entity(db, "pr #990", "pr #990")
    await _enqueue(db, a, b)
    router = _router({})  # would return distinct, but must not be called

    counts = await adj.run_adjudication_drain(db, router, mode="propose_only", budget=10)

    assert counts["mechanical_distinct"] == 1
    router.route_call.assert_not_called()
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["verdict"] == "distinct" and row["provider"] == "mechanical"


@pytest.mark.asyncio
async def test_already_judged_pair_not_rejudged(db):
    a = await _mk_entity(db, "alpha one", "alpha one")
    b = await _mk_entity(db, "alpha onee", "alpha onee")
    await adj_crud.record_verdict(db, entity_a=a, entity_b=b, verdict="distinct")
    await _enqueue(db, a, b)
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "merge"})

    counts = await adj.run_adjudication_drain(db, router, mode="propose_only", budget=10)

    assert counts["noop"] == 1 and counts["judged"] == 0
    router.route_call.assert_not_called()


@pytest.mark.asyncio
async def test_gone_entity_is_noop(db):
    a = await _mk_entity(db, "ghost", "ghost")
    b = await _mk_entity(db, "ghostt", "ghostt")
    await _enqueue(db, a, b)
    # b is merged away with no survivor target chain → resolve returns None path:
    await db.execute("UPDATE entities SET status='gone' WHERE entity_id=?", (b,))
    await db.commit()
    router = _router({"entity_adjudication": "merge"})

    counts = await adj.run_adjudication_drain(db, router, mode="propose_only", budget=10)
    assert counts["noop"] == 1
    router.route_call.assert_not_called()


@pytest.mark.asyncio
async def test_unparseable_payload_discarded(db):
    item_id = str(uuid.uuid4())
    await dw_crud.create(
        db,
        id=item_id,
        work_type=adj.WORK_TYPE,
        priority=60,
        payload_json="{not json",
        deferred_at="2026-07-17T00:00:00+00:00",
        deferred_reason="fuzzy",
        created_at="2026-07-17T00:00:00+00:00",
    )
    counts = await adj.run_adjudication_drain(db, _router({}), mode="propose_only", budget=10)
    assert counts["discarded"] == 1
    cur = await db.execute("SELECT status FROM deferred_work_queue WHERE id=?", (item_id,))
    assert (await cur.fetchone())[0] == "discarded"


@pytest.mark.asyncio
async def test_attempts_exhausted_discards_with_observation(db):
    a = await _mk_entity(db, "worn one", "worn one")
    b = await _mk_entity(db, "worn onee", "worn onee")
    item_id = await _enqueue(db, a, b)
    await db.execute("UPDATE deferred_work_queue SET attempts=5 WHERE id=?", (item_id,))
    await db.commit()

    counts = await adj.run_adjudication_drain(db, _router({}), mode="propose_only", budget=10)
    assert counts["discarded"] == 1
    cur = await db.execute("SELECT COUNT(*) FROM observations WHERE type='deferred_work_exhausted'")
    assert (await cur.fetchone())[0] == 1


# ── drain: live ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_applies_merge(db):
    a = await _mk_entity(db, "dispatch: cli", "dispatch: cli")
    b = await _mk_entity(db, "dispatch=cli", "dispatch=cli")
    # give `a` more mentions → a should survive
    await entities_crud.upsert_mention(db, memory_id="m1", entity_id=a, provenance="EXTRACTED")
    await entities_crud.upsert_mention(db, memory_id="m2", entity_id=a, provenance="EXTRACTED")
    await _enqueue(db, b, a)  # payload order (new=b, similar=a) must not dictate survivor
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "merge"})

    counts = await adj.run_adjudication_drain(db, router, mode="live", budget=10)

    assert counts["merged"] == 1
    assert await _status(db, a) == "active"  # better-attested survives
    assert await _status(db, b) == "merged"
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["verdict"] == "merge" and row["survivor_id"] == a and row["applied_at"]


@pytest.mark.asyncio
async def test_live_merge_race_guard_falls_back_to_proposed(db, monkeypatch):
    """If a side moves between resolve and the merge (extraction race), the live
    path records a proposal instead of applying a stale merge."""
    a = await _mk_entity(db, "race a", "race a")
    b = await _mk_entity(db, "race aa", "race aa")
    await _enqueue(db, a, b)
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "merge"})

    real_resolve = adj._resolve_active
    calls = {"n": 0}

    async def _flaky_resolve(db_, eid):
        calls["n"] += 1
        # First two calls (initial resolve of both) succeed; the re-check (3rd+)
        # simulates the entity having been merged away mid-drain.
        if calls["n"] > 2:
            return None
        return await real_resolve(db_, eid)

    monkeypatch.setattr(adj, "_resolve_active", _flaky_resolve)

    counts = await adj.run_adjudication_drain(db, router, mode="live", budget=10)

    assert counts["merged"] == 0 and counts["proposed"] == 1
    assert await _status(db, a) == "active" and await _status(db, b) == "active"
    assert (await adj_crud.get_by_pair(db, a, b))["verdict"] == "proposed_merge"


@pytest.mark.asyncio
async def test_live_phase0_applies_proposed_backlog(db):
    a = await _mk_entity(db, "kappa", "kappa")
    b = await _mk_entity(db, "kappaa", "kappaa")
    # a proposal recorded during a prior shadow run
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="kappa",
        norm_b="kappaa",
    )
    counts = await adj.run_adjudication_drain(db, _router({}), mode="live", budget=10)
    assert counts["merged"] == 1
    assert await _status(db, b) == "merged"
    assert (await adj_crud.get_by_pair(db, a, b))["verdict"] == "merge"


@pytest.mark.asyncio
async def test_live_phase0_stale_on_norm_drift(db):
    a = await _mk_entity(db, "lambda", "lambda")
    b = await _mk_entity(db, "lambdaa", "lambdaa")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="lambda",
        norm_b="OLD-DIFFERENT-NORM",
    )
    counts = await adj.run_adjudication_drain(db, _router({}), mode="live", budget=10)
    assert counts["stale"] == 1 and counts["merged"] == 0
    assert await _status(db, b) == "active"  # not applied
    assert (await adj_crud.get_by_pair(db, a, b))["verdict"] == "stale"


@pytest.mark.asyncio
async def test_run_observation_emitted_and_user_visible(db):
    from genesis.db.crud.observations import INTERNAL_OBS_TYPES

    a = await _mk_entity(db, "omega x", "omega x")
    b = await _mk_entity(db, "omega xx", "omega xx")
    await _enqueue(db, a, b)
    router = _router({"entity_adjudication": "merge", "entity_adjudication_challenge": "merge"})
    await adj.run_adjudication_drain(db, router, mode="propose_only", budget=10)

    cur = await db.execute("SELECT COUNT(*) FROM observations WHERE type='entity_adjudication'")
    assert (await cur.fetchone())[0] == 1
    # digest-visible: NOT excluded from the morning report
    assert "entity_adjudication" not in INTERNAL_OBS_TYPES


# ── sweep ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_enqueues_fuzzy_pair(db):
    await _mk_entity(db, "neural monitor", "neural monitor")
    await _mk_entity(db, "neural-monitor", "neural-monitor")
    await _mk_entity(db, "totally different", "totally different")

    result = await adj.run_reconcile_sweep(db, slice_size=100, enqueue_cap=50)

    assert result["enqueued"] == 1  # only the fuzzy pair
    assert result["completed"] is True
    pending = await dw_crud.query_pending(db, work_type=adj.WORK_TYPE, limit=100)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_sweep_skips_digit_pairs_and_existing_verdicts(db):
    await _mk_entity(db, "pr #100", "pr #100")
    await _mk_entity(db, "pr #101", "pr #101")
    j1 = await _mk_entity(db, "judged a", "judged a")
    j2 = await _mk_entity(db, "judged aa", "judged aa")
    await adj_crud.record_verdict(db, entity_a=j1, entity_b=j2, verdict="distinct")

    result = await adj.run_reconcile_sweep(db, slice_size=100, enqueue_cap=50)
    assert result["enqueued"] == 0  # digit pair skipped mechanically; judged pair deduped


@pytest.mark.asyncio
async def test_sweep_queues_exact_norm_cross_type_pair(db):
    # Two DIFFERENT entities sharing a norm_name across types (allowed by
    # UNIQUE(norm_name, entity_type)) are legitimate merge candidates that only
    # the sweep can recover — must be queued, not dropped (Codex P2).
    await _mk_entity(db, "omi", "omi", etype="product")
    await _mk_entity(db, "omi", "omi", etype="device")
    result = await adj.run_reconcile_sweep(db, slice_size=100, enqueue_cap=50)
    assert result["enqueued"] == 1


@pytest.mark.asyncio
async def test_sweep_rediscovers_stale_but_not_settled(db):
    # A stale pair must be re-enqueued (identity drifted → re-adjudicate); a
    # settled distinct pair must stay deduped.
    s1 = await _mk_entity(db, "stale one", "stale one")
    s2 = await _mk_entity(db, "stale onee", "stale onee")
    await adj_crud.record_verdict(db, entity_a=s1, entity_b=s2, verdict="stale")
    d1 = await _mk_entity(db, "settled one", "settled one")
    d2 = await _mk_entity(db, "settled onee", "settled onee")
    await adj_crud.record_verdict(db, entity_a=d1, entity_b=d2, verdict="distinct")

    result = await adj.run_reconcile_sweep(db, slice_size=100, enqueue_cap=50)

    assert result["enqueued"] == 1  # only the stale pair re-surfaces
    pending = await dw_crud.query_pending(db, work_type=adj.WORK_TYPE, limit=100)
    payload = json.loads(pending[0]["payload_json"])
    assert {payload["entity_id"], payload["similar_entity_id"]} == {s1, s2}


@pytest.mark.asyncio
async def test_sweep_cursor_advances_and_completes(db):
    for i in range(5):
        await _mk_entity(db, f"ent {i}", f"ent {i}")
    r1 = await adj.run_reconcile_sweep(db, slice_size=2, enqueue_cap=50, cursor_offset=0)
    assert r1["next_offset"] == 2 and r1["completed"] is False
    r2 = await adj.run_reconcile_sweep(db, slice_size=2, enqueue_cap=50, cursor_offset=4)
    assert r2["completed"] is True and r2["next_offset"] == 0


@pytest.mark.asyncio
async def test_maybe_run_sweep_low_water_gate(db):
    # Fill the queue past 2×budget so the sweep must skip.
    for i in range(10):
        await _enqueue(db, f"x{i}", f"y{i}")
    out = await adj.maybe_run_sweep(db, drain_budget=2, slice_size=100, enqueue_cap=50)
    assert out is None  # skipped: queue too deep
