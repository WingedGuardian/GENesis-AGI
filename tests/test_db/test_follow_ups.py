"""Tests for follow_ups CRUD — retention cleanup + four-axis foundation."""

import pytest

from genesis.db.crud import follow_ups

_BASE = dict(
    source="test",
    content="test follow-up",
    reason="testing",
    strategy="ego_judgment",
    priority="medium",
)


async def test_purge_completed_deletes_old(db):
    """Old completed follow-ups are deleted."""
    fid = await follow_ups.create(db, **_BASE)
    # Mark completed with an old timestamp
    await db.execute(
        "UPDATE follow_ups SET status = 'completed', "
        "completed_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
        (fid,),
    )
    await db.commit()

    count = await follow_ups.purge_completed(db)
    assert count == 1
    row = await follow_ups.get_by_id(db, fid)
    assert row is None


async def test_purge_completed_keeps_recent(db):
    """Recently completed follow-ups survive."""
    fid = await follow_ups.create(db, **_BASE)
    await follow_ups.update_status(db, fid, status="completed",
                                   resolution_notes="done")

    count = await follow_ups.purge_completed(db)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row is not None


async def test_purge_completed_keeps_pinned(db):
    """Pinned follow-ups are never purged regardless of age."""
    fid = await follow_ups.create(db, **_BASE)
    await db.execute(
        "UPDATE follow_ups SET status = 'completed', pinned = 1, "
        "completed_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
        (fid,),
    )
    await db.commit()

    count = await follow_ups.purge_completed(db)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row is not None


async def test_purge_completed_keeps_pending(db):
    """Pending follow-ups are never touched."""
    fid = await follow_ups.create(db, **_BASE)

    count = await follow_ups.purge_completed(db)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row is not None
    assert row["status"] == "pending"


async def test_purge_failed_old(db):
    """Old failed follow-ups are also purged."""
    fid = await follow_ups.create(db, **_BASE)
    await db.execute(
        "UPDATE follow_ups SET status = 'failed', "
        "completed_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
        (fid,),
    )
    await db.commit()

    count = await follow_ups.purge_completed(db)
    assert count == 1


# ─── Four-axis foundation: kind / domain / goal_id ───────────────────────────


async def test_create_with_kind_and_domain(db):
    """create() persists kind, domain, and goal_id."""
    fid = await follow_ups.create(
        db, **_BASE, kind="tabled", domain="internal", goal_id="g1",
    )
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "tabled"
    assert row["domain"] == "internal"
    assert row["goal_id"] == "g1"


async def test_create_defaults(db):
    """Defaults: kind='follow_up', domain NULL, goal_id NULL."""
    fid = await follow_ups.create(db, **_BASE)
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "follow_up"
    assert row["domain"] is None
    assert row["goal_id"] is None


async def test_get_actionable_excludes_tabled(db):
    """Tabled follow-ups never appear in the actionable feed unless opted in."""
    fu = await follow_ups.create(db, **_BASE)
    tabled = await follow_ups.create(db, **_BASE, kind="tabled")

    ids = {r["id"] for r in await follow_ups.get_actionable(db)}
    assert fu in ids
    assert tabled not in ids

    ids_all = {r["id"] for r in await follow_ups.get_actionable(db, include_tabled=True)}
    assert tabled in ids_all


async def test_get_pending_excludes_tabled(db):
    """The dispatcher/pending feeds also exclude tabled by default."""
    tabled = await follow_ups.create(db, **_BASE, kind="tabled")
    ids = {r["id"] for r in await follow_ups.get_pending(db)}
    assert tabled not in ids


async def test_set_kind_moves_lane(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.set_kind(db, fid, "tabled")
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "tabled"


async def test_set_kind_rejects_invalid(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.set_kind(db, fid, "bogus")


async def test_set_domain_and_clear(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.set_domain(db, fid, "user_world")
    assert (await follow_ups.get_by_id(db, fid))["domain"] == "user_world"
    assert await follow_ups.set_domain(db, fid, None)
    assert (await follow_ups.get_by_id(db, fid))["domain"] is None


async def test_delete_removes_row(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.delete(db, fid)
    assert await follow_ups.get_by_id(db, fid) is None


async def test_query_page_filters_search_and_null_domain(db):
    await follow_ups.create(
        db, **{**_BASE, "content": "alpha internal item"}, domain="internal",
    )
    await follow_ups.create(
        db, **{**_BASE, "content": "beta user item"}, domain="user_world",
    )
    await follow_ups.create(db, **{**_BASE, "content": "gamma unclassified"})

    internal = await follow_ups.query_page(db, domain="internal")
    assert internal and all(r["domain"] == "internal" for r in internal)

    hits = await follow_ups.query_page(db, search="alpha")
    assert len(hits) == 1 and "alpha" in hits[0]["content"]

    nulls = await follow_ups.query_page(db, domain="__null__")
    assert nulls and all(r["domain"] is None for r in nulls)

    total = await follow_ups.count_filtered(db)
    assert total == 3


async def test_query_page_rejects_nothing_on_bad_sort(db):
    """Unknown sort falls back to priority instead of raising/injecting."""
    await follow_ups.create(db, **_BASE)
    rows = await follow_ups.query_page(db, sort="; DROP TABLE follow_ups; --")
    assert len(rows) == 1


async def test_set_priority_updates(db):
    fid = await follow_ups.create(db, **_BASE)
    assert await follow_ups.set_priority(db, fid, "critical")
    assert (await follow_ups.get_by_id(db, fid))["priority"] == "critical"


async def test_set_priority_rejects_invalid(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.set_priority(db, fid, "urgent")


async def test_set_priority_unknown_id_returns_false(db):
    assert await follow_ups.set_priority(db, "nope", "high") is False


async def test_delete_batch_removes_all(db):
    ids = [await follow_ups.create(db, **_BASE) for _ in range(3)]
    assert await follow_ups.delete_batch(db, ids) == 3
    for fid in ids:
        assert await follow_ups.get_by_id(db, fid) is None
    # Empty selection is a no-op, not an error.
    assert await follow_ups.delete_batch(db, []) == 0


async def test_set_kind_batch_moves_lane(db):
    ids = [await follow_ups.create(db, **_BASE) for _ in range(2)]
    assert await follow_ups.set_kind_batch(db, ids, "tabled") == 2
    for fid in ids:
        assert (await follow_ups.get_by_id(db, fid))["kind"] == "tabled"


async def test_set_kind_batch_rejects_invalid(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.set_kind_batch(db, [fid], "bogus")


async def test_update_status_batch_stamps_completed_at(db):
    ids = [await follow_ups.create(db, **_BASE) for _ in range(2)]
    assert await follow_ups.update_status_batch(
        db, ids, "completed", resolution_notes="bulk done"
    ) == 2
    for fid in ids:
        row = await follow_ups.get_by_id(db, fid)
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["resolution_notes"] == "bulk done"


# ─── completed_at re-stamp semantics (idempotent terminal re-write) ──────────
# completed_at feeds three GC/report windows (get_recently_resolved /
# purge_completed / get_recently_completed). A notes-only status update passes
# an already-terminal status back in; it must NOT reset completed_at (which
# would silently move the row in those windows / reset the reaper clock). But a
# GENUINE transition INTO a terminal state — e.g. the ego resolving a `failed`
# follow-up to `completed` (ego/dispatch.py) — MUST stamp fresh.

_OLD = "2025-01-01T00:00:00+00:00"


async def _force_terminal(db, fid, status, completed_at=_OLD):
    await db.execute(
        "UPDATE follow_ups SET status = ?, completed_at = ? WHERE id = ?",
        (status, completed_at, fid),
    )
    await db.commit()


async def test_notes_only_rewrite_preserves_completed_at(db):
    """Re-writing a completed row with the same status (notes-only) keeps completed_at."""
    fid = await follow_ups.create(db, **_BASE)
    await _force_terminal(db, fid, "completed")
    await follow_ups.update_status(db, fid, "completed", resolution_notes="later note")
    row = await follow_ups.get_by_id(db, fid)
    assert row["completed_at"] == _OLD  # preserved, NOT re-stamped
    assert row["resolution_notes"] == "later note"


async def test_failed_to_completed_stamps_fresh(db):
    """A genuine failed→completed transition (ego resolution) stamps completed_at fresh."""
    fid = await follow_ups.create(db, **_BASE)
    await _force_terminal(db, fid, "failed")
    await follow_ups.update_status(db, fid, "completed", resolution_notes="ego resolved")
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "completed"
    # ISO-8601 sorts lexically: a fresh 2026+ stamp is strictly greater than the sentinel.
    assert row["completed_at"] > _OLD  # fresh, NOT frozen at the failure time


async def test_batch_notes_only_rewrite_preserves_completed_at(db):
    """update_status_batch mirrors: idempotent terminal re-write preserves completed_at."""
    ids = [await follow_ups.create(db, **_BASE) for _ in range(2)]
    for fid in ids:
        await _force_terminal(db, fid, "completed")
    await follow_ups.update_status_batch(db, ids, "completed", resolution_notes="bulk note")
    for fid in ids:
        row = await follow_ups.get_by_id(db, fid)
        assert row["completed_at"] == _OLD
        assert row["resolution_notes"] == "bulk note"


async def test_batch_failed_to_completed_stamps_fresh(db):
    """update_status_batch: genuine failed→completed transition stamps fresh."""
    ids = [await follow_ups.create(db, **_BASE) for _ in range(2)]
    for fid in ids:
        await _force_terminal(db, fid, "failed")
    await follow_ups.update_status_batch(db, ids, "completed")
    for fid in ids:
        row = await follow_ups.get_by_id(db, fid)
        assert row["completed_at"] > _OLD


async def test_update_status_batch_rejects_invalid_status(db):
    fid = await follow_ups.create(db, **_BASE)
    with pytest.raises(ValueError):
        await follow_ups.update_status_batch(db, [fid], "cancelled")


# ─── PR3: exact-match domain scoping on the reader queries ───────────────────
# Each reader gains an optional exact-match `domain`. Two guarantees per reader:
#   (1) domain="user_world" returns ONLY user_world rows (NULL excluded);
#   (2) domain=None (or omitted) returns IDENTICAL rows to before — the
#       load-bearing backward-compat claim for every untouched caller.


async def _seed_pending_domains(db):
    """One pending follow-up per domain. Returns {domain_key: id}."""
    return {
        "internal": await follow_ups.create(
            db, **{**_BASE, "content": "internal item"}, domain="internal",
        ),
        "user_world": await follow_ups.create(
            db, **{**_BASE, "content": "user item"}, domain="user_world",
        ),
        "null": await follow_ups.create(
            db, **{**_BASE, "content": "unclassified item"},
        ),
    }


async def test_get_pending_domain_exact_match(db):
    ids = await _seed_pending_domains(db)
    scoped = {r["id"] for r in await follow_ups.get_pending(db, domain="user_world")}
    assert scoped == {ids["user_world"]}  # NULL + internal excluded


async def test_get_pending_domain_none_is_noop(db):
    ids = await _seed_pending_domains(db)
    explicit_none = {r["id"] for r in await follow_ups.get_pending(db, domain=None)}
    unscoped = {r["id"] for r in await follow_ups.get_pending(db)}
    assert explicit_none == unscoped == set(ids.values())


async def test_get_by_status_domain_exact_match(db):
    ids = await _seed_pending_domains(db)
    scoped = {
        r["id"] for r in await follow_ups.get_by_status(db, "pending", domain="user_world")
    }
    assert scoped == {ids["user_world"]}
    unscoped = {r["id"] for r in await follow_ups.get_by_status(db, "pending")}
    assert unscoped == set(ids.values())  # backward-compat no-op


async def test_get_actionable_domain_exact_match(db):
    ids = await _seed_pending_domains(db)
    scoped = {r["id"] for r in await follow_ups.get_actionable(db, domain="user_world")}
    assert scoped == {ids["user_world"]}
    unscoped = {r["id"] for r in await follow_ups.get_actionable(db)}
    assert unscoped == set(ids.values())  # backward-compat no-op


async def test_get_recently_completed_domain_exact_match(db):
    int_id = await follow_ups.create(
        db, **{**_BASE, "content": "int done"}, domain="internal",
    )
    uw_id = await follow_ups.create(
        db, **{**_BASE, "content": "uw done"}, domain="user_world",
    )
    null_id = await follow_ups.create(db, **{**_BASE, "content": "null done"})
    await follow_ups.update_status(db, int_id, "completed")
    await follow_ups.update_status(db, uw_id, "completed")
    await follow_ups.update_status(db, null_id, "completed")

    scoped = [r["content"] for r in await follow_ups.get_recently_completed(db, domain="user_world")]
    assert scoped == ["uw done"]  # internal + NULL excluded (NB: result has no domain col)
    unscoped = {r["content"] for r in await follow_ups.get_recently_completed(db)}
    assert unscoped == {"int done", "uw done", "null done"}  # NULL survives unscoped no-op


async def test_get_actionable_domain_excludes_pinned_internal(db):
    """A PINNED internal row is still excluded by domain='user_world'. Locks the
    PR3 decision that pinned/high-priority cross-domain items re-home to the
    cockpit, NOT the user (CEO) ego."""
    uw = await follow_ups.create(
        db, **{**_BASE, "content": "user item"}, domain="user_world",
    )
    pinned_internal = await follow_ups.create(
        db, **{**_BASE, "content": "pinned internal"}, domain="internal", pinned=True,
    )
    ids = {r["id"] for r in await follow_ups.get_actionable(db, domain="user_world")}
    assert ids == {uw}
    assert pinned_internal not in ids


# ─── Cockpit: status exclusion + pin-first / actionability sort ──────────────


async def test_query_page_status_exclude_hides_terminal(db):
    """status_exclude drops the listed statuses (the cockpit 'hide done')."""
    keep = await follow_ups.create(db, **{**_BASE, "content": "still open"})
    done = await follow_ups.create(db, **{**_BASE, "content": "finished"})
    await follow_ups.update_status(db, done, status="completed")

    rows = await follow_ups.query_page(db, status_exclude=["completed", "failed"])
    ids = {r["id"] for r in rows}
    assert keep in ids and done not in ids
    assert await follow_ups.count_filtered(
        db, status_exclude=["completed", "failed"]
    ) == 1


async def test_query_page_explicit_status_overrides_exclude(db):
    """An explicit status filter wins over status_exclude (mutually exclusive)."""
    done = await follow_ups.create(db, **{**_BASE, "content": "finished"})
    await follow_ups.update_status(db, done, status="completed")

    rows = await follow_ups.query_page(
        db, status="completed", status_exclude=["completed"],
    )
    assert {r["id"] for r in rows} == {done}


async def test_query_page_pinned_floats_to_top(db):
    """Pinned rows sort first regardless of the chosen sort key."""
    pinned_old = await follow_ups.create(db, **{**_BASE, "content": "pinned old"})
    await follow_ups.create(db, **{**_BASE, "content": "newer unpinned"})
    # Make the pinned row OLDER so a plain recency/priority sort would bury it.
    await db.execute(
        "UPDATE follow_ups SET pinned = 1, "
        "created_at = '2025-01-01T00:00:00+00:00' WHERE id = ?",
        (pinned_old,),
    )
    await db.commit()

    for sort in ("priority", "created_desc", "status", "source"):
        rows = await follow_ups.query_page(db, sort=sort)
        assert rows[0]["id"] == pinned_old, f"pinned not first under sort={sort}"


async def test_query_page_status_sort_ranks_by_actionability(db):
    """Status sort surfaces active work before terminal states (not A→Z)."""
    done = await follow_ups.create(db, **{**_BASE, "content": "done"})
    active = await follow_ups.create(db, **{**_BASE, "content": "active"})
    await follow_ups.update_status(db, done, status="completed")
    await follow_ups.update_status(db, active, status="in_progress")

    order = [r["id"] for r in await follow_ups.query_page(db, sort="status")]
    # Alphabetical 'completed' < 'in_progress' would invert this — the rank CASE
    # must put in_progress first.
    assert order.index(active) < order.index(done)


# ─── Inbox attention-marker decay (WATCH/BOOKMARK tabled lane) ────────────────


async def _make_marker(db, *, age_days: int, kind="tabled",
                       source="inbox_evaluation", content="[WATCH] x",
                       status="pending"):
    """Create a follow-up, backdate created_at by *age_days*, set *status*."""
    fid = await follow_ups.create(
        db, source=source, content=content, reason="r",
        strategy="ego_judgment", priority="low", kind=kind,
    )
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    await db.execute(
        "UPDATE follow_ups SET created_at = ?, status = ? WHERE id = ?",
        (old, status, fid),
    )
    await db.commit()
    return fid


async def test_decay_stale_inbox_markers_ages_out_old(db):
    """A tabled inbox marker older than the threshold is completed with a note."""
    fid = await _make_marker(db, age_days=61)

    count = await follow_ups.decay_stale_inbox_markers(db, older_than_days=60)
    assert count == 1
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert "decayed" in (row["resolution_notes"] or "")


async def test_decay_ages_out_blocked_marker(db):
    """Regression: a BLOCKED tabled marker must decay too, else it is immortal.

    purge_completed only reaps completed/failed (which carry completed_at); a
    blocked tabled row has no completed_at, so if decay skipped it (pending-only)
    nothing would ever reap it.
    """
    fid = await _make_marker(db, age_days=61, status="blocked")

    count = await follow_ups.decay_stale_inbox_markers(db, older_than_days=60)
    assert count == 1
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "completed"
    assert "decayed" in (row["resolution_notes"] or "")


async def test_decay_skips_terminal_markers(db):
    """Already completed/failed tabled markers are left to purge_completed."""
    done = await _make_marker(db, age_days=200, status="completed")
    failed = await _make_marker(db, age_days=200, status="failed")

    count = await follow_ups.decay_stale_inbox_markers(db, older_than_days=60)
    assert count == 0
    assert (await follow_ups.get_by_id(db, done))["status"] == "completed"
    assert (await follow_ups.get_by_id(db, failed))["status"] == "failed"


async def test_decay_keeps_recent_marker(db):
    """A tabled inbox marker younger than the threshold is untouched."""
    fid = await _make_marker(db, age_days=30)

    count = await follow_ups.decay_stale_inbox_markers(db, older_than_days=60)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "pending"


async def test_decay_ignores_actionable_follow_up_lane(db):
    """An old ADOPT/ADAPT/EXPLORE follow_up (not tabled) is never decayed."""
    fid = await _make_marker(
        db, age_days=200, kind="follow_up", content="[ADAPT] x",
    )

    count = await follow_ups.decay_stale_inbox_markers(db, older_than_days=60)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "pending"


async def test_decay_ignores_non_inbox_tabled(db):
    """A tabled marker from another source (e.g. a tracked bug) is never decayed."""
    fid = await _make_marker(db, age_days=200, source="manual_bug_tracker")

    count = await follow_ups.decay_stale_inbox_markers(db, older_than_days=60)
    assert count == 0
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "pending"


async def test_get_recently_resolved_excludes_tabled(db):
    """Decayed (completed) tabled markers must not read as ego-resolved work."""
    fu = await follow_ups.create(db, **_BASE)
    await follow_ups.update_status(db, fu, status="completed")
    marker = await follow_ups.create(db, **_BASE, kind="tabled")
    await follow_ups.update_status(db, marker, status="completed")

    ids = {r["id"] for r in await follow_ups.get_recently_resolved(db)}
    assert fu in ids
    assert marker not in ids

    ids_all = {
        r["id"]
        for r in await follow_ups.get_recently_resolved(db, include_tabled=True)
    }
    assert marker in ids_all


async def test_get_recently_completed_excludes_tabled(db):
    """The daily morning-report 'Completed (24h)' path also drops tabled.

    get_recently_completed selects content/resolution_notes (not id), so assert
    on the content of a decayed marker vs a genuinely-completed follow-up.
    """
    await follow_ups.create(db, **{**_BASE, "content": "real work done"})
    fu = (await follow_ups.get_pending(db))[0]["id"]
    await follow_ups.update_status(db, fu, status="completed")
    marker = await follow_ups.create(
        db, **{**_BASE, "content": "[BOOKMARK] decayed"}, kind="tabled",
    )
    await follow_ups.update_status(db, marker, status="completed")

    contents = {r["content"] for r in await follow_ups.get_recently_completed(db)}
    assert "real work done" in contents
    assert "[BOOKMARK] decayed" not in contents

    contents_all = {
        r["content"]
        for r in await follow_ups.get_recently_completed(db, include_tabled=True)
    }
    assert "[BOOKMARK] decayed" in contents_all
