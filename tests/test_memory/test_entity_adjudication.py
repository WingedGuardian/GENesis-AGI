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


@pytest.fixture
async def file_db():
    """File-backed DB at the conftest-redirected ``genesis_db_path()``.

    The live apply path opens its OWN connection via ``get_raw_db(genesis_db_path())``;
    an ``:memory:`` database is private to a single connection, so the shared ``db``
    fixture cannot be seen by that owned connection. This fixture seeds the SAME file the
    owned connection will open, so apply-path tests exercise the real two-connection
    isolation. (The autouse ``_isolate_genesis_db_path`` conftest fixture has already
    redirected ``genesis_db_path()`` to ``tmp_path/isolated-genesis.db``.)
    """
    import aiosqlite

    from genesis.db.connection import SerializedConnection
    from genesis.db.schema import create_all_tables, seed_data
    from genesis.env import genesis_db_path

    conn = await aiosqlite.connect(str(genesis_db_path()))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await seed_data(conn)
    await conn.commit()
    wrapped = SerializedConnection(conn)
    try:
        yield wrapped
    finally:
        await wrapped.close()


async def _status_fresh(eid):
    """Read entity status on a FRESH connection to the file DB — proves COMMITTED state,
    independent of any open txn on the fixture/owned connection."""
    import aiosqlite

    from genesis.env import genesis_db_path

    conn = await aiosqlite.connect(str(genesis_db_path()))
    try:
        cur = await conn.execute("SELECT status FROM entities WHERE entity_id=?", (eid,))
        row = await cur.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


async def _verdict_fresh(pair_key):
    """Read an adjudication verdict on a FRESH connection (committed state)."""
    import aiosqlite

    from genesis.env import genesis_db_path

    conn = await aiosqlite.connect(str(genesis_db_path()))
    try:
        cur = await conn.execute(
            "SELECT verdict FROM entity_adjudications WHERE pair_key=?", (pair_key,)
        )
        row = await cur.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


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


def test_prompts_carry_qualifier_vs_compound_policy():
    """MW-3 Option-1 policy: a QUALIFIER variant that restates what the thing is
    merges (same referent), while a compound naming a distinct activity/role/
    artifact stays distinct. Both prompts must encode this so the containment
    class (which difflib never surfaced) is judged, not blindly merged."""
    for prompt in (adj._ADJUDICATION_PROMPT, adj._CHALLENGE_PROMPT):
        low = prompt.lower()
        assert "qualifier" in low
        # merge-same-thing framing AND keep-related-distinct framing both present
        assert "same" in low and ("related" in low or "distinct thing" in low)


def test_prompts_treat_version_suffixes_as_distinct():
    """Codex round-6 P1: the dotted-suffix generator nominates version-like
    pairs ("3.12" vs "1.3.12"), and an unqualified 'short form of its full
    address → merge' cue would bias BOTH models toward merging them. Both
    prompts must (a) call version-like dotted numbers DISTINCT by default and
    (b) condition the address-merge cue on snippet EVIDENCE of the same
    host/endpoint."""
    for prompt in (adj._ADJUDICATION_PROMPT, adj._CHALLENGE_PROMPT):
        low = prompt.lower()
        assert "version" in low
        assert "3.12" in prompt and "1.3.12" in prompt  # the concrete anti-example
    # The merge cue itself is evidence-conditioned, not unconditional.
    assert "snippets show" in adj._ADJUDICATION_PROMPT.lower()
    assert "snippets evidence" in adj._CHALLENGE_PROMPT.lower()


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
async def test_live_phase0_applies_proposed_backlog(file_db):
    db = file_db  # apply path opens its own get_raw_db conn → needs a file-backed DB
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
    # PR-1 gate: a shadow proposal is applied ONLY after a human approves it.
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="test")
    counts = await adj.run_adjudication_drain(db, _router({}), mode="live", budget=10)
    assert counts["merged"] == 1
    assert await _status(db, b) == "merged"
    assert (await adj_crud.get_by_pair(db, a, b))["verdict"] == "merge"


@pytest.mark.asyncio
async def test_live_phase0_stale_on_norm_drift(file_db):
    db = file_db
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
    # Even an APPROVED proposal is not applied if identity drifted — staleness
    # guard still wins over approval.
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="test")
    counts = await adj.run_adjudication_drain(db, _router({}), mode="live", budget=10)
    assert counts["stale"] == 1 and counts["merged"] == 0
    assert await _status(db, b) == "active"  # not applied
    assert (await adj_crud.get_by_pair(db, a, b))["verdict"] == "stale"


@pytest.mark.asyncio
async def test_live_backlog_skips_unapproved(db):
    """The core PR-1 gate: an UN-approved proposal is never applied, even in live."""
    a = await _mk_entity(db, "mu", "mu")
    b = await _mk_entity(db, "muu", "muu")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="mu",
        norm_b="muu",
    )
    # No approve() call → the backlog scan (approved_only) must skip it.
    counts = await adj.run_adjudication_drain(db, _router({}), mode="live", budget=10)
    assert counts["merged"] == 0
    assert await _status(db, b) == "active"  # NOT merged
    assert (await adj_crud.get_by_pair(db, a, b))["verdict"] == "proposed_merge"


@pytest.mark.asyncio
async def test_apply_approved_merges_applies_and_stamps(file_db):
    """apply_approved_merges (mode-independent) applies an approved proposal + journals it."""
    db = file_db
    a = await _mk_entity(db, "nu", "nu")
    b = await _mk_entity(db, "nuu", "nuu")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="nu",
        norm_b="nuu",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="jay")

    counts = await adj.apply_approved_merges(db, budget=10)  # no mode='live' needed

    assert counts["merged"] == 1
    assert await _status(db, b) == "merged"
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["verdict"] == "merge" and row["applied_at"]
    # reversibility snapshot written
    cur = await db.execute("SELECT loser_id FROM entity_merge_journal WHERE loser_id = ?", (b,))
    assert await cur.fetchone() is not None


@pytest.mark.asyncio
async def test_reject_records_distinct_and_not_applied(db):
    """Rejecting a proposal makes it 'distinct' (never applied) and drops approval."""
    a = await _mk_entity(db, "xi", "xi")
    b = await _mk_entity(db, "xii", "xii")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
    )
    moved = await adj_crud.reject(db, pair_key=adj_crud.pair_key(a, b), reason="different things")
    assert moved is True
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["verdict"] == "distinct" and row["approved_at"] is None
    # A distinct pair is settled → excluded from re-nomination.
    assert adj_crud.pair_key(a, b) in await adj_crud.settled_pair_keys(db)
    # And it is never applied.
    counts = await adj.apply_approved_merges(db, budget=10)
    assert counts["merged"] == 0
    assert await _status(db, b) == "active"


@pytest.mark.asyncio
async def test_reapprove_not_clobbered_by_readjudication(db):
    """A re-adjudication of an already-approved pair must NOT wipe the approval."""
    a = await _mk_entity(db, "omicron", "omicron")
    b = await _mk_entity(db, "omicronn", "omicronn")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="omicron",
        norm_b="omicronn",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="jay")
    # Re-judge records a fresh proposed_merge on the same pair (upsert on pair_key).
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="omicron",
        norm_b="omicronn",
        provider="strong-model",
    )
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["approved_at"] is not None and row["approved_by"] == "jay"
    assert row["provider"] == "strong-model"  # the re-judge DID update other fields


@pytest.mark.asyncio
async def test_stale_voids_approval_no_silent_reapply(file_db):
    """BLOCKER regression: a drifted APPROVED proposal is marked stale AND loses its
    approval, so it cannot silently re-apply after re-adjudication under norms the
    human never saw. mark_stale is the only transition that re-opens an approved row."""
    db = file_db
    a = await _mk_entity(db, "john smith", "john smith")
    b = await _mk_entity(db, "jon smith", "jon smith")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="john smith",
        norm_b="jon smith",
    )
    pk = adj_crud.pair_key(a, b)
    await adj_crud.approve(db, pair_key=pk, approved_by="jay")
    # `a` drifts (rename) before apply — still active, so it will re-pair later.
    await db.execute("UPDATE entities SET norm_name='john q smith' WHERE entity_id=?", (a,))
    await db.commit()

    counts = await adj.apply_approved_merges(db, budget=10)
    assert counts["stale"] == 1 and counts["merged"] == 0
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["verdict"] == "stale"
    assert row["approved_at"] is None and row["approved_by"] is None  # approval VOIDED

    # Re-adjudication re-proposes the pair (record_verdict preserves approval if any —
    # but there is none now, so the re-proposed row is UN-approved).
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="john q smith",
        norm_b="jon smith",
    )
    assert (await adj_crud.get_by_pair(db, a, b))["approved_at"] is None
    # And it does NOT apply without a fresh human approval.
    counts2 = await adj.apply_approved_merges(db, budget=10)
    assert counts2["merged"] == 0
    assert await _status(db, b) == "active"


@pytest.mark.asyncio
async def test_apply_batch_isolates_failing_row(file_db, monkeypatch):
    """A row that raises in merge_entity is rolled back + skipped; other approved rows
    in the same batch still apply, and the failed row's partial write is discarded."""
    db = file_db
    g1 = await _mk_entity(db, "good a", "good a")
    g2 = await _mk_entity(db, "good b", "good b")
    await adj_crud.record_verdict(
        db,
        entity_a=g1,
        entity_b=g2,
        verdict="proposed_merge",
        loser_id=g2,
        survivor_id=g1,
        norm_a="good a",
        norm_b="good b",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(g1, g2), approved_by="jay")
    b1 = await _mk_entity(db, "bad a", "bad a")
    b2 = await _mk_entity(db, "bad b", "bad b")
    await adj_crud.record_verdict(
        db,
        entity_a=b1,
        entity_b=b2,
        verdict="proposed_merge",
        loser_id=b2,
        survivor_id=b1,
        norm_a="bad a",
        norm_b="bad b",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(b1, b2), approved_by="jay")

    real_merge = entities_crud.merge_entity

    async def _flaky(db_, *, loser_id, survivor_id, _commit=True):
        if loser_id == b2:  # write a partial row, then raise → must be rolled back
            await db_.execute(
                "INSERT INTO entity_mentions "
                "(memory_id, entity_id, provenance, confidence, source, created_at) "
                "VALUES ('partial', ?, 'EXTRACTED', 0.9, 's', '2026-01-01T00:00:00+00:00')",
                (survivor_id,),
            )
            raise RuntimeError("boom mid-merge")
        return await real_merge(db_, loser_id=loser_id, survivor_id=survivor_id, _commit=_commit)

    monkeypatch.setattr(entities_crud, "merge_entity", _flaky)

    counts = await adj.apply_approved_merges(db, budget=10)
    assert counts["merged"] == 1 and counts.get("errors", 0) == 1
    assert await _status(db, g2) == "merged"  # good pair applied
    assert await _status(db, b2) == "active"  # failed pair NOT merged
    # the failed row's partial mention was rolled back
    cur = await db.execute("SELECT count(*) FROM entity_mentions WHERE memory_id='partial'")
    assert (await cur.fetchone())[0] == 0


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
async def test_sweep_cap_overflow_advances_cursor_and_resurfaces_next_pass(db):
    # Cap-hit overflow is DEFERRED, never lost: the cursor still advances (a
    # park-on-cap variant was reverted 2026-08-12 — exhausted/`discarded` pairs
    # are in neither settled nor pending keys, so a parked offset re-nominates
    # them forever and wedges the sweep). On the next pass over the same slice,
    # already-pending pairs are excluded and the overflow surfaces.
    await _mk_entity(db, "svc", "svc")
    for q in ("alpha", "beta", "gamma"):
        await _mk_entity(db, f"svc {q}", f"svc {q}")
    # 3 containment pairs (svc ⊂ each qualifier). cap=2 → first pass enqueues 2
    # and the cursor ADVANCES normally (no parking).
    r1 = await adj.run_reconcile_sweep(db, slice_size=100, enqueue_cap=2, cursor_offset=0)
    assert r1["enqueued"] == 2
    assert r1["completed"] is True  # slice covered the whole set → pass completes
    # A later pass re-scans from 0; the 2 pending pairs are excluded → the 3rd
    # surfaces. Overflow latency (up to one sweep cycle) is the accepted tradeoff;
    # the convergence redesign is PR-2b's immediate re-enqueue, not cursor tricks.
    r2 = await adj.run_reconcile_sweep(db, slice_size=100, enqueue_cap=2, cursor_offset=0)
    assert r2["enqueued"] == 1
    pending = await dw_crud.query_pending(db, work_type=adj.WORK_TYPE, limit=100)
    assert len(pending) == 3  # all 3 eventually enqueued, none dropped


@pytest.mark.asyncio
async def test_maybe_run_sweep_low_water_gate(db):
    # Fill the queue past 2×budget so the sweep must skip.
    for i in range(10):
        await _enqueue(db, f"x{i}", f"y{i}")
    out = await adj.maybe_run_sweep(db, drain_budget=2, slice_size=100, enqueue_cap=50)
    assert out is None  # skipped: queue too deep


# ── safety-critical apply-path rails (remediation) ───────────────────────────


async def _add_mentions(db, entity_id, n):
    """Give an entity ``n`` distinct mentions so count_entity_mentions() diverges."""
    for i in range(n):
        await entities_crud.upsert_mention(
            db,
            memory_id=f"mem-{entity_id}-{i}",
            entity_id=entity_id,
            provenance="EXTRACTED",
            confidence=0.7,
        )


@pytest.mark.asyncio
async def test_apply_honors_stored_direction_not_recomputed_survivor(file_db):
    """apply must tombstone the STORED loser — never re-pick from live mention counts.

    The human approved survivor=a / loser=b. Then b accrues MORE mentions than a,
    so a survivor RECOMPUTED from current counts (_pick_survivor) would flip the
    direction and tombstone `a` — the entity the human chose to KEEP. Honoring the
    stored direction keeps `a` active and merges `b`. (Verified-RED: with the old
    recompute this asserts the opposite outcome and fails.)
    """
    db = file_db
    a = await _mk_entity(db, "keep", "keep")
    b = await _mk_entity(db, "keepp", "keepp")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,  # stored, human-approved direction: keep a, drop b
        survivor_id=a,
        norm_a="keep",
        norm_b="keepp",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="jay")
    # Counts now DISAGREE with the stored direction: b is better-attested.
    await _add_mentions(db, b, 3)
    assert await entities_crud.count_entity_mentions(
        db, b
    ) > await entities_crud.count_entity_mentions(db, a)

    counts = await adj.apply_approved_merges(db, budget=10)

    assert counts["merged"] == 1
    assert await _status(db, a) == "active"  # the human's survivor is kept
    assert await _status(db, b) == "merged"  # the stored loser is tombstoned
    row = await adj_crud.get_by_pair(db, a, b)
    assert row["survivor_id"] == a and row["loser_id"] == b


@pytest.mark.asyncio
async def test_claim_approved_for_apply_is_atomic_single_winner(db):
    """The conditional claim flips proposed_merge→merge exactly once.

    A second claim (concurrent double-apply) loses; a claim after a reject also
    loses. This is the TOCTOU / double-apply lock at the SQL boundary.
    """
    a = await _mk_entity(db, "cl", "cl")
    b = await _mk_entity(db, "cll", "cll")
    pk = adj_crud.pair_key(a, b)
    await adj_crud.record_verdict(
        db, entity_a=a, entity_b=b, verdict="proposed_merge", loser_id=b, survivor_id=a
    )
    await adj_crud.approve(db, pair_key=pk, approved_by="jay")

    first = await adj_crud.claim_approved_for_apply(db, pair_key=pk, loser_id=b, survivor_id=a)
    second = await adj_crud.claim_approved_for_apply(db, pair_key=pk, loser_id=b, survivor_id=a)
    assert first is True and second is False
    assert (await adj_crud.get_by_pair(db, a, b))["verdict"] == "merge"


@pytest.mark.asyncio
async def test_claim_fails_after_reject(db):
    """A reject landing before the claim (read-then-apply TOCTOU) blocks the apply."""
    a = await _mk_entity(db, "rj", "rj")
    b = await _mk_entity(db, "rjj", "rjj")
    pk = adj_crud.pair_key(a, b)
    await adj_crud.record_verdict(
        db, entity_a=a, entity_b=b, verdict="proposed_merge", loser_id=b, survivor_id=a
    )
    await adj_crud.approve(db, pair_key=pk, approved_by="jay")
    await adj_crud.reject(db, pair_key=pk, reason="not the same")
    claimed = await adj_crud.claim_approved_for_apply(db, pair_key=pk, loser_id=b, survivor_id=a)
    assert claimed is False
    assert await _status(db, b) == "active"  # never merged


@pytest.mark.asyncio
async def test_apply_negative_budget_applies_nothing(db):
    """A negative budget (SQLite LIMIT -1 = unlimited) must apply NOTHING, not everything."""
    a = await _mk_entity(db, "nb", "nb")
    b = await _mk_entity(db, "nbb", "nbb")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="nb",
        norm_b="nbb",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="jay")

    counts = await adj.apply_approved_merges(db, budget=-1)

    assert counts["merged"] == 0
    assert await _status(db, b) == "active"  # the approved merge was NOT bulk-applied
    # And the crud query itself refuses to dump the table on a negative limit.
    assert await adj_crud.list_proposed_merges(db, limit=-1, approved_only=True) == []


@pytest.mark.asyncio
async def test_apply_stale_when_stored_direction_absent(file_db):
    """A proposal missing a stored survivor/loser can't be honored → marked stale."""
    db = file_db
    a = await _mk_entity(db, "sd", "sd")
    b = await _mk_entity(db, "sdd", "sdd")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=None,
        survivor_id=None,
        norm_a="sd",
        norm_b="sdd",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="jay")

    counts = await adj.apply_approved_merges(db, budget=10)

    assert counts["merged"] == 0 and counts["stale"] == 1
    assert await _status(db, b) == "active"


# ── owned-connection isolation: foreign commit + CancelledError (#6/#7) ────────


@pytest.mark.asyncio
async def test_apply_foreign_commit_cannot_flush_partial_merge(file_db, monkeypatch):
    """#6: the apply runs on its OWN connection, so a foreign commit landing mid-merge
    (the MCP middleware, or another coroutine on the shared conn) can NOT durably commit
    THIS row's partial merge. Verified-RED against the shared-connection SAVEPOINT code —
    there the foreign commit flushes the open savepoint's partial write."""
    db = file_db
    a = await _mk_entity(db, "iso keep", "iso keep")
    b = await _mk_entity(db, "iso lose", "iso lose")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="iso keep",
        norm_b="iso lose",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="test")

    async def _foreign_commit_then_fail(conn, *, loser_id, survivor_id, _commit=True):
        # Partial destructive write, THEN a foreign commit on the SHARED conn, THEN fail.
        await conn.execute(
            "UPDATE entities SET status='merged', merged_into=? WHERE entity_id=?",
            (survivor_id, loser_id),
        )
        await db.commit()  # foreign commit — would flush a shared-conn savepoint's partial
        raise RuntimeError("merge failed after a foreign commit")

    monkeypatch.setattr(entities_crud, "merge_entity", _foreign_commit_then_fail)

    counts = await adj.apply_approved_merges(db, budget=10)

    assert counts.get("errors", 0) == 1
    # Read on a FRESH connection: the partial merge must NOT have survived.
    assert await _status_fresh(b) != "merged"


@pytest.mark.asyncio
async def test_apply_cancelled_mid_merge_rolls_back(file_db, monkeypatch):
    """#7: a CancelledError during merge_entity is a BaseException that bypasses
    `except Exception`; the owned transaction must still roll back so NO claim and NO
    partial merge survive — even if a commit follows. Verified-RED against the
    `except Exception` + shared-conn savepoint code (the claim + partial stay in the open
    savepoint and a following commit flushes them)."""
    import asyncio

    db = file_db
    a = await _mk_entity(db, "cx keep", "cx keep")
    b = await _mk_entity(db, "cx lose", "cx lose")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a="cx keep",
        norm_b="cx lose",
    )
    await adj_crud.approve(db, pair_key=adj_crud.pair_key(a, b), approved_by="test")

    async def _partial_then_cancel(conn, *, loser_id, survivor_id, _commit=True):
        await conn.execute(
            "UPDATE entities SET status='merged', merged_into=? WHERE entity_id=?",
            (survivor_id, loser_id),
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(entities_crud, "merge_entity", _partial_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await adj.apply_approved_merges(db, budget=10)
    await db.commit()  # a following commit must NOT flush a leaked partial/claim

    assert await _status_fresh(b) != "merged"  # loser not tombstoned
    assert await _verdict_fresh(adj_crud.pair_key(a, b)) == "proposed_merge"  # claim rolled back
