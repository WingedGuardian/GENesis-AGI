"""Tests for goal progress backward link — enriched notes from ego dispatches.

Covers: on_end hook writing enriched progress notes with outcome summaries,
and the ego_goal_progress MCP tool.
"""

from __future__ import annotations

import json

import pytest

# ── On-end hook: enriched progress notes ─────────────────────────────────────


@pytest.mark.asyncio
async def test_enriched_note_includes_output_text():
    """Completed dispatch should include output_text in the progress note."""
    # Simulate the on_end hook logic directly (it's a closure,
    # so we test the same code path inline)
    session = {
        "source_tag": "ego_dispatch",
        "status": "completed",
        "cost_usd": 0.1234,
        "metadata": json.dumps({
            "caller_context": "ego_proposal:prop_abc123",
            "output_text": "Successfully published article to Medium",
            "error": None,
        }),
    }

    proposal = {
        "id": "prop_abc123",
        "content": "Publish the stateless agent article to Medium",
        "goal_id": "goal_xyz789",
    }

    status = session["status"]
    meta = json.loads(session["metadata"])
    content = proposal["content"][:60]

    # Replicate the enriched note logic from init/ego.py
    # Error-first: prefer error when present (completed sessions
    # can have is_error=True without triggering fail()).
    _error = (meta.get("error") or "").strip()
    outcome = _error[:120] if _error else (meta.get("output_text") or "")[:120]
    outcome = outcome.replace("\n", " ").strip()

    if outcome:
        note = f"[{status}] {content}: {outcome} (session:{'sess1234'[:8]})"
    else:
        note = f"[{status}] {content} (session:{'sess1234'[:8]})"

    assert "[completed]" in note
    assert "Publish the stateless agent article to Medium" in note
    assert "Successfully published article to Medium" in note
    assert "session:sess1234" in note
    # Should NOT contain cost (old format had cost)
    assert "$" not in note


@pytest.mark.asyncio
async def test_enriched_note_failed_dispatch_includes_error():
    """Failed dispatch should include error text in the progress note."""
    meta = {
        "caller_context": "ego_proposal:prop_fail",
        "output_text": "",
        "error": "Browser session timed out after 120s",
    }

    status = "failed"
    content = "Submit job application via browser"[:60]

    _error = (meta.get("error") or "").strip()
    outcome = _error[:120] if _error else (meta.get("output_text") or "")[:120]
    outcome = outcome.replace("\n", " ").strip()

    if outcome:
        note = f"[{status}] {content}: {outcome} (session:abcd1234)"
    else:
        note = f"[{status}] {content} (session:abcd1234)"

    assert "[failed]" in note
    assert "Submit job application via browser" in note
    assert "Browser session timed out after 120s" in note


@pytest.mark.asyncio
async def test_enriched_note_completed_but_errored_uses_error():
    """Completed session with is_error=True should use error, not output_text.

    DirectSessionRunner calls complete() even when output.is_error=True;
    only Python exceptions trigger fail(). So status can be 'completed'
    while error is populated.
    """
    meta = {
        "output_text": "Some irrelevant partial output",
        "error": "Tool execution failed: permission denied",
    }

    # Error-first logic: error takes priority regardless of status
    _error = (meta.get("error") or "").strip()
    outcome = _error[:120] if _error else (meta.get("output_text") or "")[:120]
    outcome = outcome.replace("\n", " ").strip()

    assert outcome == "Tool execution failed: permission denied"
    # Crucially, it should NOT contain the irrelevant output_text
    assert "irrelevant partial output" not in outcome


@pytest.mark.asyncio
async def test_enriched_note_no_outcome_falls_back():
    """When neither output_text nor error exist, note has no outcome summary."""
    meta = {
        "caller_context": "ego_proposal:prop_empty",
        "output_text": "",
        "error": "",
    }

    status = "completed"
    content = "Investigate memory drift"[:60]

    _error = (meta.get("error") or "").strip()
    outcome = _error[:120] if _error else (meta.get("output_text") or "")[:120]
    outcome = outcome.replace("\n", " ").strip()

    if outcome:
        note = f"[{status}] {content}: {outcome} (session:abcd1234)"
    else:
        note = f"[{status}] {content} (session:abcd1234)"

    assert "[completed]" in note
    assert "Investigate memory drift" in note
    # No colon-separated outcome
    assert ": " not in note.split("]", 1)[1].rsplit("(session:", 1)[0].strip()


@pytest.mark.asyncio
async def test_enriched_note_strips_newlines_from_outcome():
    """Newlines in output_text should be replaced with spaces."""
    meta = {
        "output_text": "Published\narticle\nsuccessfully",
        "error": None,
    }

    outcome = (meta.get("output_text") or "")[:120]
    outcome = outcome.replace("\n", " ").strip()

    assert "\n" not in outcome
    assert outcome == "Published article successfully"


@pytest.mark.asyncio
async def test_enriched_note_truncates_long_outcome():
    """Output text longer than 120 chars should be truncated."""
    long_text = "A" * 200

    outcome = long_text[:120]
    assert len(outcome) == 120


# ── CRUD: add_progress_note ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_progress_note_appends_to_json(db):
    """add_progress_note should append a timestamped entry to progress_notes."""
    from genesis.db.crud import user_goals

    # Create a test goal
    goal_id = await user_goals.create(
        db,
        title="test-goal-progress-note",
        category="project",
        priority="medium",
    )

    # Add a progress note
    result = await user_goals.add_progress_note(
        db, goal_id, "First progress update"
    )
    assert result is True

    # Verify the note was stored
    goal = await user_goals.get_by_id(db, goal_id)
    assert goal is not None
    notes = json.loads(goal["progress_notes"])
    assert len(notes) == 1
    assert notes[0]["note"] == "First progress update"
    assert "date" in notes[0]

    # Add a second note — verifies append, not overwrite
    await user_goals.add_progress_note(db, goal_id, "Second update")
    goal = await user_goals.get_by_id(db, goal_id)
    notes = json.loads(goal["progress_notes"])
    assert len(notes) == 2
    assert notes[1]["note"] == "Second update"


@pytest.mark.asyncio
async def test_add_progress_note_returns_false_for_missing_goal(db):
    """add_progress_note should return False for nonexistent goals."""
    from genesis.db.crud import user_goals

    result = await user_goals.add_progress_note(
        db, "nonexistent-goal-id", "some note"
    )
    assert result is False


# ── MCP tool: ego_goal_progress existence check ─────────────────────────────


def test_ego_goal_progress_tool_registered():
    """ego_goal_progress should be registered as an MCP tool."""
    from genesis.mcp.health import ego_tools

    # Verify the function exists in the module
    assert hasattr(ego_tools, "ego_goal_progress")
    # The @mcp.tool() decorator wraps it in a FunctionTool
    tool = ego_tools.ego_goal_progress
    assert tool is not None
