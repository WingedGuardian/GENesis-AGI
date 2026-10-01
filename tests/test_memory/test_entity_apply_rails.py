"""MW-3 PR-2b apply-safety rails.

Four rails, one premise: the moment merges APPLY, redirect chains and
merged-away surface forms become live states every reader must handle —
and the sweep's weeks-long rediscovery loop becomes the convergence
bottleneck. Each rail is RED-locked against the pre-rail behavior:

- chain-safe follow: ``resolve_active`` (shared), multi-hop
  ``get_by_norm_name``, and read-side walks (``merge_entity`` no longer
  re-points chains; it leaves them for the walks to follow);
- typed fold lookup: a person/org sharing a norm can no longer shadow the
  concept-cluster fold (review NOTE N2);
- query-lane merge-following: a merged-away surface form resolves to its
  survivor instead of going dark;
- immediate stale re-enqueue: a norm-drift stale pair re-enters the queue
  at apply time instead of waiting for the weekly sweep;
- policy stamping + pre-policy re-open (``settled_pair_keys``).
"""

from __future__ import annotations

import pytest

from genesis.db.crud import entities as entities_crud
from genesis.db.crud import entity_adjudications as adj_crud
from genesis.memory import entity_query


async def _mk(db, name, norm, etype="concept"):
    return await entities_crud.create_entity(
        db, name=name, norm_name=norm, entity_type=etype
    )


async def _tombstone(db, loser_id, survivor_id):
    """Hand-write a merged tombstone WITHOUT merge_entity, so chain tests can
    build exact multi-hop shapes directly."""
    await db.execute(
        "UPDATE entities SET status='merged', merged_into=? WHERE entity_id=?",
        (survivor_id, loser_id),
    )
    await db.commit()


# ── chain-safe follow ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_active_follows_multi_hop_chain(db):
    a = await _mk(db, "Alpha", "alpha")
    b = await _mk(db, "Beta", "beta")
    c = await _mk(db, "Gamma", "gamma")
    await _tombstone(db, a, b)
    await _tombstone(db, b, c)
    resolved = await entities_crud.resolve_active(db, a)
    assert resolved is not None and resolved["entity_id"] == c


@pytest.mark.asyncio
async def test_resolve_active_is_cycle_safe(db):
    a = await _mk(db, "Alpha", "alpha")
    b = await _mk(db, "Beta", "beta")
    await _tombstone(db, a, b)
    await _tombstone(db, b, a)  # corrupt cycle
    assert await entities_crud.resolve_active(db, a) is None


@pytest.mark.asyncio
async def test_get_by_norm_name_follows_chains_to_the_active_survivor(db):
    """Single-hop follow returned a STILL-MERGED row once chains formed —
    mentions then attached to a tombstone."""
    a = await _mk(db, "Alpha", "alpha")
    b = await _mk(db, "Beta", "beta")
    c = await _mk(db, "Gamma", "gamma")
    await _tombstone(db, a, b)
    await _tombstone(db, b, c)
    row = await entities_crud.get_by_norm_name(db, norm_name="alpha")
    assert row is not None
    assert row["entity_id"] == c, f"returned a non-terminal row: {row['status']}"
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_merge_entity_preserves_existing_chains(db):
    """Inbound redirects are NOT re-pointed: A→loser stays A→loser and the
    read-side walk follows it to the new survivor. The old compaction rewrote
    A→C without journaling A→B, so an unmerge could not restore the redirect
    (Codex P2, #1729) — write-side chains may now be >1 hop, which the walks
    are built for."""
    a = await _mk(db, "Alpha", "alpha")
    b = await _mk(db, "Beta", "beta")
    c = await _mk(db, "Gamma", "gamma")
    await _tombstone(db, a, b)  # pre-existing chain a→b
    await entities_crud.merge_entity(db, loser_id=b, survivor_id=c)
    row = await entities_crud.get_entity(db, a)
    assert row["merged_into"] == b, "the inbound redirect must survive the merge"
    assert (await entities_crud.resolve_active(db, a))["entity_id"] == c


# ── typed fold lookup ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_typed_lookup_ignores_a_shadowing_person(db):
    """A person created FIRST shares the norm; the untyped lookup's top row is
    the person, and the old fold rejected — minting an avoidable shard."""
    from genesis.memory.entity_registry import _CONCEPT_CLUSTER

    await _mk(db, "Atlas", "atlas", etype="person")
    concept = await _mk(db, "Atlas", "atlas", etype="concept")
    row = await entities_crud.get_by_norm_name_in_types(
        db, norm_name="atlas", types=_CONCEPT_CLUSTER
    )
    assert row is not None and row["entity_id"] == concept


@pytest.mark.asyncio
async def test_typed_lookup_empty_types_returns_none(db):
    await _mk(db, "Atlas", "atlas", etype="concept")
    assert (
        await entities_crud.get_by_norm_name_in_types(db, norm_name="atlas", types=set())
        is None
    )


@pytest.mark.asyncio
async def test_fold_reuses_cluster_row_despite_person_shadow(db):
    """Registry-level: the Tier-2 cross-type fold reaches the cluster row even
    when a person owns the untyped lookup's top slot."""
    from genesis.memory import entity_registry

    await _mk(db, "Atlas", "atlas", etype="person")
    concept = await _mk(db, "Atlas", "atlas", etype="concept")
    eid, prov = await entity_registry.resolve_entity(
        db, name="Atlas", entity_type="product", aliases={}
    )
    assert eid == concept, "the person shadow defeated the cluster fold"
    assert prov == "EXTRACTED"


# ── query-lane merge-following ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_merged_norm_redirects_chase_chains_and_drop_dead_ends(db):
    a = await _mk(db, "Old Name", "old name")
    b = await _mk(db, "Mid Name", "mid name")
    c = await _mk(db, "New Name", "new name")
    await _tombstone(db, a, b)
    await _tombstone(db, b, c)
    dead = await _mk(db, "Dead", "dead")
    await _tombstone(db, dead, "no-such-entity")
    redirects = await entities_crud.merged_norm_redirects(db)
    assert redirects.get("old name") == [c]
    assert redirects.get("mid name") == [c]
    assert "dead" not in redirects
    # No MERGED row bears "new name" (it is only the survivor's own active
    # norm), so it is never a redirect key. (An active norm that a merged row
    # ALSO bears DOES redirect now — see the same-norm test below.)
    assert "new name" not in redirects


@pytest.mark.asyncio
async def test_query_lane_resolves_a_merged_away_surface_form(db):
    """The day-one gap in entity_query's own docstring: after a merge applies,
    a query naming the OLD surface form went dark."""
    loser = await _mk(db, "omi", "omi")
    survivor = await _mk(db, "omi device", "omi device")
    await _tombstone(db, loser, survivor)
    weights = await entity_query.resolve_query_entities(db, "what about omi lately")
    assert survivor in weights, "the merged-away surface form no longer resolves"


# ── policy stamping + pre-policy re-open ─────────────────────────────────


@pytest.mark.asyncio
async def test_record_verdict_stamps_the_current_policy(db):
    await adj_crud.record_verdict(db, entity_a="e1", entity_b="e2", verdict="distinct")
    row = await adj_crud.get_by_pair(db, "e1", "e2")
    assert row["policy"] == adj_crud.POLICY_VERSION


@pytest.mark.asyncio
async def test_rejudged_pre_policy_row_is_stamped(db):
    """The infinite-loop trap, designed out at plan time: a re-judged row left
    at NULL policy would stay outside settled_pair_keys and be re-nominated
    every sweep, eating the whole drain budget."""
    await adj_crud.record_verdict(db, entity_a="e1", entity_b="e2", verdict="distinct")
    await db.execute(
        "UPDATE entity_adjudications SET policy = NULL WHERE pair_key = ?",
        (adj_crud.pair_key("e1", "e2"),),
    )
    await db.commit()
    await adj_crud.record_verdict(db, entity_a="e1", entity_b="e2", verdict="distinct")
    row = await adj_crud.get_by_pair(db, "e1", "e2")
    assert row["policy"] == adj_crud.POLICY_VERSION, "conflict-update left policy NULL"


@pytest.mark.asyncio
async def test_settled_reopens_pre_policy_distinct_only(db):
    """NULL-policy 'distinct' re-opens; stamped distinct and NULL-policy merge
    stay settled; stale stays excluded (pre-existing)."""
    for a, b, verdict in (
        ("p1", "p2", "distinct"),   # will be un-stamped → re-opened
        ("q1", "q2", "distinct"),   # stays stamped → settled
        ("r1", "r2", "merge"),      # will be un-stamped, but merge → settled
        ("s1", "s2", "stale"),      # excluded regardless
    ):
        await adj_crud.record_verdict(db, entity_a=a, entity_b=b, verdict=verdict)
    for key in (adj_crud.pair_key("p1", "p2"), adj_crud.pair_key("r1", "r2")):
        await db.execute(
            "UPDATE entity_adjudications SET policy = NULL WHERE pair_key = ?", (key,)
        )
    await db.commit()
    settled = await adj_crud.settled_pair_keys(db)
    assert adj_crud.pair_key("p1", "p2") not in settled, "pre-policy distinct stayed settled"
    assert adj_crud.pair_key("q1", "q2") in settled
    assert adj_crud.pair_key("r1", "r2") in settled, "a merge must never re-open"
    assert adj_crud.pair_key("s1", "s2") not in settled


@pytest.mark.asyncio
async def test_policy_migration_adds_column_and_is_idempotent(tmp_path):
    """Both build paths carry the column: create_all_tables (the shared db
    fixture, exercised by every test above) and the ALTER migration for
    existing installs — which must also no-op when the column exists."""
    import importlib

    import aiosqlite

    mig = importlib.import_module(
        "genesis.db.migrations.20260905155727_entity_adjudication_policy"
    )
    db = await aiosqlite.connect(tmp_path / "old.db")
    try:
        # An OLD-schema table: everything but policy.
        await db.execute(
            "CREATE TABLE entity_adjudications ("
            "id TEXT PRIMARY KEY, pair_key TEXT NOT NULL UNIQUE, "
            "entity_a TEXT NOT NULL, entity_b TEXT NOT NULL, verdict TEXT NOT NULL, "
            "created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT)"
        )
        await mig.up(db)
        cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(entity_adjudications)")}
        assert "policy" in cols
        await mig.up(db)  # idempotent
        await db.commit()
    finally:
        await db.close()


# ── PR #1729 review round 1: redirect preservation + enqueue truth ───────


@pytest.mark.asyncio
async def test_redirects_survive_a_same_norm_active_entity_of_another_type(db):
    """``UNIQUE(norm_name, entity_type)`` means a merged-away concept's norm
    can still be owned by an ACTIVE entity of another type. The redirect must
    be preserved ALONGSIDE that active row — suppressing it makes the merged
    entity unfindable by its old surface form, while the query map is
    list-valued and dedups on union anyway (Codex P2, PR #1729 round 1)."""
    await _mk(db, "Atlas", "atlas", etype="person")
    concept = await _mk(db, "Atlas", "atlas", etype="concept")
    device = await _mk(db, "Atlas device", "atlas device", etype="device")
    await _tombstone(db, concept, device)

    redirects = await entities_crud.merged_norm_redirects(db)
    assert redirects.get("atlas") == [device]


@pytest.mark.asyncio
async def test_query_resolves_both_the_active_and_the_merged_same_norm_entity(db):
    """End-to-end cover for the redirect, which no test had.

    The fix for the round-1 P2 was in the PRODUCER — it deleted a
    ``norm_name in active_norms`` skip from ``merged_norm_redirects`` — and the
    sibling test above pins it correctly: restoring that skip turns it RED
    (measured). Nothing here rescues an unheld fix, and an earlier version of
    this docstring wrongly claimed it did, on the strength of a mutation to the
    CONSUMER that the fix commit never touched.

    What was genuinely uncovered is the consumer. ``resolve_query_entities``
    unions the survivor into the active map rather than overwriting it, which
    is what makes the producer's output reach a caller — and
    ``test_entity_query.py`` has no merge-following coverage at all, so
    replacing that union with an overwrite breaks the user-visible behaviour
    with every existing test still green (measured). That gap is what this
    closes.
    """
    person = await _mk(db, "Atlas", "atlas", etype="person")
    concept = await _mk(db, "Atlas", "atlas", etype="concept")
    device = await _mk(db, "Atlas device", "atlas device", etype="device")
    await _tombstone(db, concept, device)

    weights = await entity_query.resolve_query_entities(db, "Atlas")

    assert person in weights, "the active same-norm entity must still resolve"
    assert device in weights, (
        "the merged entity's survivor must resolve by the OLD surface form — "
        "dropping it is the defect this pins"
    )
    assert concept not in weights, "a tombstone is never itself a live target"


@pytest.mark.asyncio
async def test_one_norm_carries_every_survivor_it_was_merged_into(db):
    """The producer's LIST-valuedness, which its docstring argues and nothing
    pinned.

    ``UNIQUE(norm_name, entity_type)`` lets the same norm exist on two merged
    rows of different types, each with its OWN survivor. Returning a single id
    would make the result depend on scan order — nondeterministic, and silently
    dropping one survivor. A `dict[str, str]` shape passes every other test in
    this file, so without this the docstring is the only thing holding it.
    """
    concept = await _mk(db, "Atlas", "atlas", etype="concept")
    person = await _mk(db, "Atlas", "atlas", etype="person")
    device = await _mk(db, "Atlas device", "atlas device", etype="device")
    org = await _mk(db, "Atlas Corp", "atlas corp", etype="org")
    await _tombstone(db, concept, device)
    await _tombstone(db, person, org)

    redirects = await entities_crud.merged_norm_redirects(db)

    assert sorted(redirects["atlas"]) == sorted([device, org])


# The consumer's `survivor_id not in ids` dedup is deliberately NOT pinned, and
# that is a finding rather than an omission. `resolve_query_entities` returns a
# dict keyed by entity_id, so a duplicated id collapses on assignment and the
# check has NO observable effect through the public API: removing it leaves the
# whole file green (measured). A test asserting it would pass with or without
# the code it names — the shape this suite spent the round removing. The dedup
# is defensive only, and the producer's docstring should not lean on it as the
# reason list-valuedness is safe; the dict is that reason.


@pytest.mark.asyncio
async def test_enqueue_adjudication_reports_whether_it_inserted(db, monkeypatch):
    """The enqueue helper must tell callers whether a row actually landed:
    both silent no-op paths (pending-row dedup, kill switch) previously
    returned None, letting callers count phantom enqueues (Codex P2,
    PR #1729 round 1)."""
    a = await _mk(db, "eps one", "eps one")
    b = await _mk(db, "eps onee", "eps onee")
    assert await entities_crud.enqueue_adjudication(db, entity_id=a, similar_entity_id=b) is True
    # Same pair, reversed orientation → deduped, nothing inserted.
    assert await entities_crud.enqueue_adjudication(db, entity_id=b, similar_entity_id=a) is False
    # Kill switch → suppressed, nothing inserted.
    monkeypatch.setattr(entities_crud, "_ADJUDICATION_ENQUEUE_ENABLED", False)
    c = await _mk(db, "eps other", "eps other")
    assert await entities_crud.enqueue_adjudication(db, entity_id=a, similar_entity_id=c) is False


# ── round-4 fixes (review station) ───────────────────────────────────────


async def _queue_malformed(db):
    """A pending adjudication row whose payload is not JSON — the processor's
    own failure model discards these, so producers must not trip over one."""
    await db.execute(
        "INSERT INTO deferred_work_queue (id, work_type, call_site_id, priority, "
        "payload_json, deferred_at, deferred_reason, created_at) "
        "VALUES ('bad-1', 'entity_adjudication', 'entity_adjudication', 60, "
        "'{broken', '2026-01-01', 'test', '2026-01-01')"
    )
    await db.commit()


@pytest.mark.asyncio
async def test_malformed_pending_payload_does_not_block_enqueue(db):
    """json_extract raises on malformed JSON; an unguarded pair-dedup made one
    bad pending row abort every later enqueue (Codex P2 + Devin, #1729)."""
    await _queue_malformed(db)
    a = await _mk(db, "zeta one", "zeta one")
    b = await _mk(db, "zeta onee", "zeta onee")
    assert await entities_crud.enqueue_adjudication(db, entity_id=a, similar_entity_id=b) is True


@pytest.mark.asyncio
async def test_malformed_pending_payload_does_not_block_stale_flag_upgrade(db):
    """The stale-recheck flag upgrade is the other json_extract site."""
    await _queue_malformed(db)
    a = await _mk(db, "zeta two", "zeta two")
    b = await _mk(db, "zeta twoo", "zeta twoo")
    assert await entities_crud.enqueue_adjudication(db, entity_id=a, similar_entity_id=b) is True
    # Duplicate with the flag → no insert, flag stamped on the pending row.
    assert (
        await entities_crud.enqueue_adjudication(
            db, entity_id=b, similar_entity_id=a, stale_recheck=True
        )
        is False
    )
    import json

    rows = await db.execute_fetchall(
        "SELECT id, payload_json FROM deferred_work_queue "
        "WHERE work_type = 'entity_adjudication' AND status = 'pending'"
    )
    by_id = {r[0]: r[1] for r in rows}
    assert by_id.pop("bad-1") == "{broken", "the malformed row must be left untouched"
    (valid,) = by_id.values()
    assert json.loads(valid)["stale_recheck"] is True, "the flag never reached the pending row"


@pytest.mark.asyncio
async def test_typed_lookup_never_returns_a_dead_end_tombstone(db):
    """A merged cluster row whose redirect dead-ends is not a live identity;
    returning it attached extraction work to a tombstone (Codex P2, #1729)."""
    await _mk(db, "Atlas", "atlas", etype="person")
    dead = await _mk(db, "Atlas", "atlas", etype="concept")
    await _tombstone(db, dead, "no-such-entity")
    from genesis.memory.entity_registry import _CONCEPT_CLUSTER

    assert (
        await entities_crud.get_by_norm_name_in_types(
            db, norm_name="atlas", types=_CONCEPT_CLUSTER
        )
        is None
    )


@pytest.mark.asyncio
async def test_typed_lookup_skips_a_dead_end_to_a_live_row(db):
    """Regression guard: a live row of another type in the set wins over a
    dead-end tombstone. (Active rows sort first, so this holds before and after
    the candidate walk; the walk's own case is the all-merged test below.)"""
    dead = await _mk(db, "Atlas", "atlas", etype="concept")
    await _tombstone(db, dead, "no-such-entity")
    live = await _mk(db, "Atlas", "atlas", etype="product")
    from genesis.memory.entity_registry import _CONCEPT_CLUSTER

    row = await entities_crud.get_by_norm_name_in_types(
        db, norm_name="atlas", types=_CONCEPT_CLUSTER
    )
    assert row is not None and row["entity_id"] == live


@pytest.mark.asyncio
async def test_merged_norm_redirects_long_chain_resolves_every_link(db):
    """Memoized resolution must give the same answer as walking each chain:
    every merged link of a long chain redirects to the one survivor."""
    ids = [await _mk(db, f"Link {i}", f"link {i}") for i in range(60)]
    for i in range(59):
        await _tombstone(db, ids[i], ids[i + 1])
    redirects = await entities_crud.merged_norm_redirects(db)
    for i in range(59):
        assert redirects.get(f"link {i}") == [ids[59]]
    assert "link 59" not in redirects


@pytest.mark.asyncio
async def test_typed_lookup_tries_the_next_merged_candidate(db):
    """All candidates merged: a dead end sorting first must not hide a second
    merged row that does resolve to a live survivor."""
    dead = await _mk(db, "Atlas", "atlas", etype="concept")
    await _tombstone(db, dead, "no-such-entity")
    other = await _mk(db, "Atlas", "atlas", etype="product")
    survivor = await _mk(db, "Atlas Prime", "atlas prime", etype="product")
    await _tombstone(db, other, survivor)
    from genesis.memory.entity_registry import _CONCEPT_CLUSTER

    row = await entities_crud.get_by_norm_name_in_types(
        db, norm_name="atlas", types=_CONCEPT_CLUSTER
    )
    assert row is not None and row["entity_id"] == survivor


@pytest.mark.asyncio
async def test_resolve_entity_concept_folds_past_a_dead_end_tombstone(db):
    """Codex's own example, through the CALLER: extraction resolves with
    entity_type="concept"; a same-type dead-end tombstone must not be accepted
    when a live cluster row shares the norm (#1729 round-4 review)."""
    from genesis.memory import entity_registry

    dead = await _mk(db, "Atlas", "atlas", etype="concept")
    await _tombstone(db, dead, "no-such-entity")
    live = await _mk(db, "Atlas", "atlas", etype="product")
    eid, _prov = await entity_registry.resolve_entity(
        db, name="Atlas", entity_type="concept", aliases={}
    )
    assert eid == live, "a concept extraction attached to a dead-end tombstone"
