"""MW-3 PR-2b apply-safety rails.

Four rails, one premise: the moment merges APPLY, redirect chains and
merged-away surface forms become live states every reader must handle —
and the sweep's weeks-long rediscovery loop becomes the convergence
bottleneck. Each rail is RED-locked against the pre-rail behavior:

- chain-safe follow: ``resolve_active`` (shared), multi-hop
  ``get_by_norm_name``, and ``merge_entity`` chain re-pointing;
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
    build the exact multi-hop shape merge_entity's re-pointing would collapse."""
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
async def test_merge_entity_repoints_existing_chains(db):
    """Union-find compaction: rows pointing at the loser point straight at the
    new survivor after the merge, keeping write-side chains one hop."""
    a = await _mk(db, "Alpha", "alpha")
    b = await _mk(db, "Beta", "beta")
    c = await _mk(db, "Gamma", "gamma")
    await _tombstone(db, a, b)  # pre-existing chain a→b
    await entities_crud.merge_entity(db, loser_id=b, survivor_id=c)
    row = await entities_crud.get_entity(db, a)
    assert row["merged_into"] == c, "an old loser still pointed at the new tombstone"


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
