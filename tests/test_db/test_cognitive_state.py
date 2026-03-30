"""Tests for cognitive_state CRUD operations."""

from __future__ import annotations

import json

# ── Session patch reader/clearer tests ─────────────────────────────────────


async def test_load_session_patches_returns_entries(db, tmp_path):
    """load_session_patches reads patches from file."""
    from genesis.db.crud import cognitive_state

    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text(json.dumps([
        {"session_id": "abc123", "ended_at": "2026-03-26T05:00:00+00:00",
         "topic": "Fixed memory leak", "message_count": 8},
        {"session_id": "def456", "ended_at": "2026-03-26T07:00:00+00:00",
         "topic": "Resolved security issue", "message_count": 5},
    ]))

    patches = cognitive_state.load_session_patches(patches_file)
    assert len(patches) == 2
    assert patches[0]["topic"] == "Fixed memory leak"


async def test_load_session_patches_missing_file(db, tmp_path):
    """Returns empty list when file does not exist."""
    from genesis.db.crud import cognitive_state

    patches = cognitive_state.load_session_patches(tmp_path / "nope.json")
    assert patches == []


async def test_load_session_patches_corrupt_file(db, tmp_path):
    """Returns empty list on corrupt JSON."""
    from genesis.db.crud import cognitive_state

    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text("not json{{{")

    patches = cognitive_state.load_session_patches(patches_file)
    assert patches == []


async def test_clear_session_patches(db, tmp_path):
    """clear_session_patches removes the file."""
    from genesis.db.crud import cognitive_state

    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text(json.dumps([{"session_id": "x", "topic": "test"}]))

    cognitive_state.clear_session_patches(patches_file)
    assert not patches_file.exists()


async def test_clear_session_patches_missing_file(db, tmp_path):
    """No error when file does not exist."""
    from genesis.db.crud import cognitive_state

    cognitive_state.clear_session_patches(tmp_path / "nope.json")  # Should not raise


# ── Activity tier render gating tests ──────────────────────────────────────


async def test_render_active_tier_shows_only_flags_and_patches(db, tmp_path):
    """Active tier: skip narrative + pending, show flags + patches."""
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Big stale narrative about old bugs.",
        section="active_context", generated_by="deep_reflection",
        created_at="2026-03-25T10:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-2", content="Do X, Y, Z.",
        section="pending_actions", generated_by="deep_reflection",
        created_at="2026-03-25T10:00:00+00:00",
    )
    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text(json.dumps([
        {"session_id": "abc", "ended_at": "2026-03-26T05:00:00+00:00",
         "topic": "Fixed the old bugs", "message_count": 8},
    ]))

    rendered = await cognitive_state.render(
        db, activity_tier="active", patches_file=patches_file,
    )
    assert "Big stale narrative" not in rendered
    assert "Do X, Y, Z" not in rendered
    assert "Fixed the old bugs" in rendered


async def test_render_returning_tier_skips_active_context(db, tmp_path):
    """Returning tier: show pending_actions + focus + patches, skip active_context."""
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Big stale narrative.",
        section="active_context", generated_by="deep_reflection",
        created_at="2026-03-25T10:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-2", content="Do X, Y, Z.",
        section="pending_actions", generated_by="deep_reflection",
        created_at="2026-03-25T10:00:00+00:00",
    )
    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text(json.dumps([
        {"session_id": "abc", "ended_at": "2026-03-26T05:00:00+00:00",
         "topic": "Fixed things", "message_count": 5},
    ]))

    rendered = await cognitive_state.render(
        db, activity_tier="returning", patches_file=patches_file,
    )
    assert "Big stale narrative" not in rendered
    assert "Do X, Y, Z" in rendered
    assert "Fixed things" in rendered


async def test_render_away_tier_shows_everything(db, tmp_path):
    """Away tier (default): full render including active_context."""
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Full catch-up narrative.",
        section="active_context", generated_by="deep_reflection",
        created_at="2026-03-25T10:00:00+00:00",
    )
    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text(json.dumps([
        {"session_id": "abc", "ended_at": "2026-03-26T05:00:00+00:00",
         "topic": "Recent work", "message_count": 3},
    ]))

    rendered = await cognitive_state.render(
        db, activity_tier="away", patches_file=patches_file,
    )
    assert "Full catch-up narrative" in rendered
    assert "Recent work" in rendered


async def test_render_default_tier_is_away(db):
    """No tier argument = away = full render (backwards compat)."""
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Full narrative.",
        section="active_context", generated_by="test",
        created_at="2026-03-25T10:00:00+00:00",
    )
    rendered = await cognitive_state.render(db)
    assert "Full narrative" in rendered


async def test_render_active_tier_shows_fresh_narrative(db, tmp_path):
    """Active tier still shows narrative if written AFTER the last session patch."""
    from genesis.db.crud import cognitive_state

    # Narrative at 08:00, patch ended at 07:00 — narrative is NEWER
    await cognitive_state.create(
        db, id="cs-1", content="Fresh narrative from mid-day reflection.",
        section="active_context", generated_by="deep_reflection",
        created_at="2026-03-26T08:00:00+00:00",
    )
    patches_file = tmp_path / "session_patches.json"
    patches_file.write_text(json.dumps([
        {"session_id": "abc", "ended_at": "2026-03-26T07:00:00+00:00",
         "topic": "Earlier work", "message_count": 5},
    ]))

    rendered = await cognitive_state.render(
        db, activity_tier="active", patches_file=patches_file,
    )
    assert "Fresh narrative from mid-day reflection" in rendered
    assert "Earlier work" in rendered


# ── Original CRUD tests ───────────────────────────────────────────────────


async def test_create_and_get(db):
    from genesis.db.crud import cognitive_state

    row_id = await cognitive_state.create(
        db, id="cs-1", content="User is working on Genesis Phase 4.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    assert row_id == "cs-1"
    row = await cognitive_state.get_by_id(db, "cs-1")
    assert row is not None
    assert row["section"] == "active_context"
    assert row["generated_by"] == "glm5"


async def test_get_by_section(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Active context here.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-2", content="Pending actions here.",
        section="pending_actions", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    rows = await cognitive_state.get_by_section(db, "active_context")
    assert len(rows) == 1
    assert rows[0]["content"] == "Active context here."


async def test_get_current_returns_latest(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-old", content="Old context.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T08:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-new", content="New context.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    row = await cognitive_state.get_current(db, "active_context")
    assert row is not None
    assert row["id"] == "cs-new"


async def test_render_includes_stored_sections_and_focus(db):
    """render() includes active_context, pending_actions, and focus directive."""
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="Working on Phase 4.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    await cognitive_state.create(
        db, id="cs-2", content="Draft user.md after Phase 4.",
        section="pending_actions", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    # Focus directive stored in state_flags section
    await cognitive_state.create(
        db, id="cs-3",
        content="## Deep Reflection Focus Directive\nFix memory retrieval.",
        section="state_flags", generated_by="deep_reflection",
        created_at="2026-03-05T10:00:00+00:00",
    )
    rendered = await cognitive_state.render(db)
    assert "Working on Phase 4." in rendered
    assert "Draft user.md after Phase 4." in rendered
    assert "Fix memory retrieval." in rendered


async def test_render_empty_returns_bootstrap(db):
    from genesis.db.crud import cognitive_state

    rendered = await cognitive_state.render(db)
    assert "No cognitive state yet" in rendered


async def test_delete(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-1", content="temp",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T10:00:00+00:00",
    )
    deleted = await cognitive_state.delete(db, "cs-1")
    assert deleted is True
    assert await cognitive_state.get_by_id(db, "cs-1") is None


async def test_replace_section(db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.create(
        db, id="cs-old", content="Old.",
        section="active_context", generated_by="glm5",
        created_at="2026-03-05T08:00:00+00:00",
    )
    await cognitive_state.replace_section(
        db, section="active_context", id="cs-new",
        content="Replaced.", generated_by="claude-sonnet",
        created_at="2026-03-05T10:00:00+00:00",
    )
    rows = await cognitive_state.get_by_section(db, "active_context")
    assert len(rows) == 1
    assert rows[0]["id"] == "cs-new"
    assert rows[0]["content"] == "Replaced."


# ── Computed state flags tests ──────────────────────────────────────────────


async def test_compute_flags_memory_retrieval_failure(db):
    """Flag appears when observations exist but none have been retrieved."""
    from genesis.db.crud import cognitive_state, observations

    # Create unresolved observations with retrieved_count=0 (default)
    for i in range(3):
        await observations.create(
            db, id=f"obs-{i}", source="test", type="test",
            content=f"Test observation {i}", priority="medium",
            created_at="2026-03-05T10:00:00+00:00",
        )

    flags = await cognitive_state.compute_state_flags(db)
    assert "MEMORY RETRIEVAL FAILURE" in flags
    assert "3 unresolved observations, 0 retrieved" in flags


async def test_compute_flags_memory_retrieval_ok(db):
    """Flag disappears when at least one observation has been retrieved."""
    from genesis.db.crud import cognitive_state, observations

    await observations.create(
        db, id="obs-1", source="test", type="test",
        content="Test observation", priority="medium",
        created_at="2026-03-05T10:00:00+00:00",
    )
    # Simulate retrieval
    await observations.increment_retrieved(db, "obs-1")

    flags = await cognitive_state.compute_state_flags(db)
    assert "MEMORY RETRIEVAL FAILURE" not in flags


async def test_compute_flags_memory_backlog(db):
    """Flag appears when too many observations in last 24h."""
    from datetime import UTC, datetime

    from genesis.db.crud import cognitive_state, observations

    now = datetime.now(UTC).isoformat()
    for i in range(25):
        await observations.create(
            db, id=f"obs-{i}", source="test", type="test",
            content=f"Observation {i}", priority="medium",
            created_at=now,
        )

    flags = await cognitive_state.compute_state_flags(db)
    assert "MEMORY BACKLOG" in flags


async def test_compute_flags_memory_backlog_excludes_resolved(db):
    """Resolved observations should NOT count toward the backlog flag."""
    from datetime import UTC, datetime

    from genesis.db.crud import cognitive_state, observations

    now = datetime.now(UTC).isoformat()
    # Create 25 observations, then resolve 20 of them
    for i in range(25):
        await observations.create(
            db, id=f"obs-resolved-{i}", source="test", type="test",
            content=f"Resolved observation {i}", priority="medium",
            created_at=now,
        )
    for i in range(20):
        await observations.resolve(
            db, id=f"obs-resolved-{i}",
            resolved_at=now, resolution_notes="test cleanup",
        )

    # Only 5 unresolved remain — below the 20 threshold
    flags = await cognitive_state.compute_state_flags(db)
    assert "MEMORY BACKLOG" not in flags


async def test_compute_flags_job_failure(db):
    """Flag appears when a job has consecutive failures."""
    from genesis.db.crud import cognitive_state

    # Insert a failed job directly into job_health table
    await db.execute(
        "INSERT INTO job_health (job_name, last_run, consecutive_failures, last_error, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("weekly_assessment", "2026-03-05T10:00:00", 3, "TimeoutError", "2026-03-05T10:00:00"),
    )
    await db.commit()

    flags = await cognitive_state.compute_state_flags(db)
    assert "JOB FAILURE" in flags
    assert "weekly_assessment" in flags
    assert "3 consecutive failures" in flags


async def test_compute_flags_empty_when_healthy(db):
    """No flags when system is healthy."""
    from genesis.db.crud import cognitive_state

    flags = await cognitive_state.compute_state_flags(db)
    assert flags == ""


async def test_render_includes_computed_flags(db):
    """render() includes computed health flags alongside stored sections."""
    from genesis.db.crud import cognitive_state, observations

    await cognitive_state.create(
        db, id="cs-1", content="Active context.",
        section="active_context", generated_by="test",
        created_at="2026-03-05T10:00:00+00:00",
    )
    # Create observations that will trigger the retrieval flag
    for i in range(3):
        await observations.create(
            db, id=f"obs-{i}", source="test", type="test",
            content=f"Obs {i}", priority="medium",
            created_at="2026-03-05T10:00:00+00:00",
        )

    rendered = await cognitive_state.render(db)
    assert "Active context." in rendered
    assert "MEMORY RETRIEVAL FAILURE" in rendered


async def test_render_auto_clears_resolved_flags(db):
    """render() no longer shows flags when conditions resolve."""
    from genesis.db.crud import cognitive_state, observations

    # Create observation (will trigger retrieval flag)
    await observations.create(
        db, id="obs-1", source="test", type="test",
        content="Test", priority="medium",
        created_at="2026-03-05T10:00:00+00:00",
    )

    # Before retrieval — flag present
    rendered = await cognitive_state.render(db)
    assert "MEMORY RETRIEVAL FAILURE" in rendered

    # After retrieval — flag gone
    await observations.increment_retrieved(db, "obs-1")
    rendered = await cognitive_state.render(db)
    assert "MEMORY RETRIEVAL FAILURE" not in rendered
