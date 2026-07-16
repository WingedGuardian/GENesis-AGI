"""Tests for observations CRUD."""

import sqlite3

import pytest

from genesis.db.crud import observations

_COMMON = dict(
    source="sensor",
    type="metric",
    content="cpu at 90%",
    priority="high",
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await observations.create(db, id="o1", **_COMMON)
    assert rid == "o1"
    row = await observations.get_by_id(db, "o1")
    assert row is not None
    assert row["priority"] == "high"


async def test_get_nonexistent(db):
    assert await observations.get_by_id(db, "nope") is None


async def test_query_no_filters(db):
    await observations.create(db, id="o2", **_COMMON)
    rows = await observations.query(db)
    assert len(rows) >= 1


async def test_query_by_source(db):
    await observations.create(db, id="o3", **_COMMON)
    await observations.create(db, id="o4", **{**_COMMON, "source": "other"})
    rows = await observations.query(db, source="sensor")
    assert all(r["source"] == "sensor" for r in rows)


async def test_query_by_source_prefix(db):
    await observations.create(db, id="sp1", **{**_COMMON, "source": "session:abc-123"})
    await observations.create(db, id="sp2", **{**_COMMON, "source": "session:def-456"})
    await observations.create(db, id="sp3", **_COMMON)
    rows = await observations.query(db, source_prefix="session:")
    assert {r["id"] for r in rows} == {"sp1", "sp2"}


async def test_query_source_filters_mutually_exclusive(db):
    with pytest.raises(ValueError):
        await observations.query(db, source="a", source_prefix="b")
    with pytest.raises(ValueError):
        await observations.query(db, source_in=["a"], source_prefix="b")


async def test_distinct_unresolved_types_and_sources(db):
    await observations.create(db, id="du1", **_COMMON)
    await observations.create(db, id="du2", **{**_COMMON, "source": "session:abc"})
    await observations.create(db, id="du3", **{**_COMMON, "type": "anomaly"})
    await observations.resolve(db, "du3", resolved_at="2026-01-02T00:00:00", resolution_notes="")
    assert await observations.distinct_unresolved_types(db) == ["metric"]
    assert await observations.distinct_unresolved_sources(db) == ["sensor", "session:abc"]


async def test_count_unsurfaced_mirrors_get_unsurfaced(db):
    await observations.create(db, id="cu1", **_COMMON)  # counted
    await observations.create(
        db, id="cu2", **{**_COMMON, "priority": "low"}
    )  # excluded by priority
    await observations.create(
        db, id="cu3", **{**_COMMON, "type": "internal_thing"}
    )  # excluded by type
    await observations.create(db, id="cu4", **_COMMON)
    await observations.mark_surfaced(db, ["cu4"], "2026-01-02T00:00:00")  # surfaced
    await observations.create(db, id="cu5", **_COMMON)
    await observations.resolve(db, "cu5", resolved_at="2026-01-02T00:00:00", resolution_notes="")

    count = await observations.count_unsurfaced(
        db,
        priority_filter=("critical", "high", "medium"),
        exclude_types=("internal_thing",),
    )
    assert count == 1
    rows = await observations.get_unsurfaced(
        db,
        priority_filter=("critical", "high", "medium"),
        exclude_types=("internal_thing",),
        limit=100,
    )
    assert count == len(rows)
    assert await observations.count_unsurfaced(db, priority_filter=()) == 0


async def test_distinct_unresolved_sources_excludes_types(db):
    """A source whose unresolved rows are ALL excluded types must not appear."""
    await observations.create(db, id="dx1", **_COMMON)
    await observations.create(
        db, id="dx2", **{**_COMMON, "source": "session:abc", "type": "conversation_pivot"}
    )
    sources = await observations.distinct_unresolved_sources(
        db, exclude_types=("conversation_pivot",)
    )
    assert sources == ["sensor"]


async def test_query_by_priority(db):
    await observations.create(db, id="o5", **{**_COMMON, "priority": "low"})
    rows = await observations.query(db, priority="low")
    assert all(r["priority"] == "low" for r in rows)


async def test_query_by_resolved(db):
    await observations.create(db, id="o6", **_COMMON)
    rows = await observations.query(db, resolved=False)
    assert all(r["resolved"] == 0 for r in rows)


async def test_resolve(db):
    await observations.create(db, id="o7", **_COMMON)
    assert (
        await observations.resolve(db, "o7", resolved_at="2026-01-02", resolution_notes="fixed")
        is True
    )
    row = await observations.get_by_id(db, "o7")
    assert row["resolved"] == 1


async def test_resolve_nonexistent(db):
    assert await observations.resolve(db, "nope", resolved_at="x", resolution_notes="x") is False


async def test_resolve_by_content_hash(db):
    """resolve_by_content_hash resolves only rows matching source + content_hash."""
    await observations.create(
        db,
        id="pf-a",
        source="routing",
        type="provider_failure",
        content="provider a down",
        priority="high",
        created_at="2026-01-01T00:00:00",
        content_hash="hash-a",
    )
    await observations.create(
        db,
        id="pf-b",
        source="routing",
        type="provider_failure",
        content="provider b down",
        priority="high",
        created_at="2026-01-01T00:00:00",
        content_hash="hash-b",
    )
    n = await observations.resolve_by_content_hash(
        db,
        source="routing",
        content_hash="hash-a",
        resolved_at="2026-01-02",
        resolution_notes="recovered",
    )
    assert n == 1
    assert (await observations.get_by_id(db, "pf-a"))["resolved"] == 1
    assert (await observations.get_by_id(db, "pf-b"))["resolved"] == 0
    # Idempotent — re-running resolves nothing more.
    assert (
        await observations.resolve_by_content_hash(
            db,
            source="routing",
            content_hash="hash-a",
            resolved_at="2026-01-02",
            resolution_notes="recovered",
        )
        == 0
    )


async def test_increment_retrieved(db):
    await observations.create(db, id="o8", **_COMMON)
    assert await observations.increment_retrieved(db, "o8") is True
    row = await observations.get_by_id(db, "o8")
    assert row["retrieved_count"] == 1


async def test_increment_retrieved_nonexistent(db):
    assert await observations.increment_retrieved(db, "nope") is False


async def test_delete(db):
    await observations.create(db, id="o9", **_COMMON)
    assert await observations.delete(db, "o9") is True
    assert await observations.get_by_id(db, "o9") is None


async def test_delete_nonexistent(db):
    assert await observations.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await observations.create(db, id="odup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await observations.create(db, id="odup", **_COMMON)


# ─── person_id tests ─────────────────────────────────────────────────────────


async def test_person_id_defaults_to_none(db):
    await observations.create(db, id="opid1", **_COMMON)
    row = await observations.get_by_id(db, "opid1")
    assert row["person_id"] is None


async def test_create_with_person_id(db):
    await observations.create(db, id="opid2", person_id="user-42", **_COMMON)
    row = await observations.get_by_id(db, "opid2")
    assert row["person_id"] == "user-42"


async def test_query_filters_by_person_id(db):
    await observations.create(db, id="opid3", person_id="alice", **_COMMON)
    await observations.create(db, id="opid4", person_id="bob", **_COMMON)
    rows = await observations.query(db, person_id="alice")
    assert len(rows) == 1
    assert rows[0]["id"] == "opid3"


async def test_query_by_category(db):
    await observations.create(db, id="ocat1", category="recon", **_COMMON)
    await observations.create(db, id="ocat2", category="learning", **_COMMON)
    rows = await observations.query(db, category="recon")
    assert len(rows) == 1
    assert rows[0]["id"] == "ocat1"


async def test_create_and_upsert_carry_origin_class(db):
    from genesis.db.crud import observations as obs

    await obs.create(
        db,
        id="o-ext",
        source="reflection",
        type="user_model_delta",
        content="{}",
        priority="medium",
        created_at="2026-01-01T00:00:00+00:00",
        origin_class="external_untrusted",
    )
    cur = await db.execute("SELECT origin_class FROM observations WHERE id='o-ext'")
    assert (await cur.fetchone())[0] == "external_untrusted"

    await obs.upsert(
        db,
        id="o-up",
        source="s",
        type="t",
        content="c",
        priority="low",
        created_at="2026-01-01T00:00:00+00:00",
        origin_class="first_party",
    )
    await obs.upsert(
        db,
        id="o-up",
        source="s",
        type="t",
        content="c2",
        priority="low",
        created_at="2026-01-01T00:00:00+00:00",
        origin_class="external_untrusted",
    )
    cur = await db.execute("SELECT origin_class, content FROM observations WHERE id='o-up'")
    row = await cur.fetchone()
    assert row[0] == "external_untrusted" and row[1] == "c2"  # conflict path updates


async def test_create_origin_class_defaults_null(db):
    from genesis.db.crud import observations as obs

    await obs.create(
        db,
        id="o-null",
        source="s",
        type="t",
        content="c",
        priority="low",
        created_at="2026-01-01T00:00:00+00:00",
    )
    cur = await db.execute("SELECT origin_class FROM observations WHERE id='o-null'")
    assert (await cur.fetchone())[0] is None


async def test_count_external_by_ids(db):
    from genesis.db.crud import observations as obs

    for oid, oc in [
        ("o-c1", "external_untrusted"),
        ("o-c2", "first_party"),
        ("o-c3", None),
        ("o-c4", "external_untrusted"),
    ]:
        await obs.create(
            db,
            id=oid,
            source="s",
            type="t",
            content="c",
            priority="low",
            created_at="2026-01-01T00:00:00+00:00",
            origin_class=oc,
        )
    # Only the external rows among the GIVEN ids count; NULL reads first-party.
    assert await obs.count_external_by_ids(db, ["o-c1", "o-c2", "o-c3"]) == 1
    assert await obs.count_external_by_ids(db, ["o-c1", "o-c4"]) == 2
    assert await obs.count_external_by_ids(db, ["o-c2", "o-c3"]) == 0
    assert await obs.count_external_by_ids(db, []) == 0
    assert await obs.count_external_by_ids(db, ["missing"]) == 0


def test_process_reaper_would_kill_ttl_registered(caplog):
    """The dry-run reaper emits `process_reaper_would_kill` (audit-trail
    counterpart to `process_reaper_kill`). It must be explicitly registered in
    _TTL_BY_TYPE so it resolves to a real TTL WITHOUT logging the recurring
    'Unknown observation type' warning on every reaper tick."""
    import logging
    from datetime import timedelta

    assert "process_reaper_would_kill" in observations._TTL_BY_TYPE
    with caplog.at_level(logging.WARNING, logger="genesis.db.crud.observations"):
        ttl = observations._compute_ttl("process_reaper_would_kill")
    assert ttl == timedelta(days=14)
    assert not any("Unknown observation type" in r.getMessage() for r in caplog.records), (
        "a registered type must not trigger the unknown-type warning"
    )


_GIT_ALERT = dict(
    source="git_health_monitor",
    type="infrastructure_alert",
    content="git alert",
    priority="critical",
    created_at="2026-01-01T00:00:00",
)


async def test_resolve_by_source_and_type_category_scoped(db):
    # cheap-scan alert, deep-scan alert, and a legacy row with NULL category
    await observations.create(db, id="g1", **{**_GIT_ALERT, "category": "git_cheap"})
    await observations.create(
        db, id="g2", **{**_GIT_ALERT, "category": "git_deep", "content": "deep alert"}
    )
    await observations.create(db, id="g3", **{**_GIT_ALERT, "content": "legacy alert"})

    n = await observations.resolve_by_source_and_type(
        db,
        source="git_health_monitor",
        type="infrastructure_alert",
        category="git_cheap",
        resolved_at="2026-01-02T00:00:00",
        resolution_notes="cheap probe passed",
    )

    # Only the matching-category row resolves; deep + legacy NULL stay open
    # (a passing structural probe must never clear a content-corruption alert).
    assert n == 1
    assert (await observations.get_by_id(db, "g1"))["resolved"] == 1
    assert (await observations.get_by_id(db, "g2"))["resolved"] == 0
    assert (await observations.get_by_id(db, "g3"))["resolved"] == 0


async def test_resolve_by_source_and_type_unscoped_clears_all_categories(db):
    await observations.create(db, id="g4", **{**_GIT_ALERT, "category": "git_cheap"})
    await observations.create(
        db, id="g5", **{**_GIT_ALERT, "category": "git_deep", "content": "deep alert"}
    )
    await observations.create(db, id="g6", **{**_GIT_ALERT, "content": "legacy alert"})

    n = await observations.resolve_by_source_and_type(
        db,
        source="git_health_monitor",
        type="infrastructure_alert",
        resolved_at="2026-01-02T00:00:00",
        resolution_notes="deep fsck passed",
    )

    # Unscoped (deep-verified) resolve clears every open git alert,
    # including pre-category legacy rows.
    assert n == 3
    for oid in ("g4", "g5", "g6"):
        assert (await observations.get_by_id(db, oid))["resolved"] == 1


async def test_skip_if_duplicate_is_atomic_single_statement(db):
    """The dedup INSERT must be one INSERT…WHERE NOT EXISTS statement, not a
    SELECT-then-INSERT — two processes can both pass a separate pre-check
    before either commits (Codex P2, PR #1085). With the atomic form, the
    second insert is a no-op regardless of interleaving."""
    r1 = await observations.create(
        db, id="atomic1", skip_if_duplicate=True, **_GIT_ALERT
    )
    r2 = await observations.create(
        db, id="atomic2", skip_if_duplicate=True, **_GIT_ALERT
    )
    assert r1 == "atomic1"
    assert r2 is None  # duplicate skipped
    cur = await db.execute(
        "SELECT count(*) FROM observations WHERE source = 'git_health_monitor'"
    )
    assert (await cur.fetchone())[0] == 1

    # After the first is resolved, an identical alert may be created again
    # (a recurrence after recovery is a NEW incident, not a duplicate).
    await observations.resolve(
        db, "atomic1", resolved_at="2026-01-02", resolution_notes="recovered"
    )
    r3 = await observations.create(
        db, id="atomic3", skip_if_duplicate=True, **_GIT_ALERT
    )
    assert r3 == "atomic3"
