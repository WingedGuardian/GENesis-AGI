"""Shared proposal-resolution hook + decision propagation (migration 0066).

The hook must produce ONE artifact set regardless of entry point; decision
rows must be captured on reject-with-reason, deduped on repeat, rendered in
an always-on context section, and retirable only by the user.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import create_all_tables
from genesis.ego.resolution import decision_prefix, handle_proposal_resolution


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _proposal(db, *, pid="p1", ego_source="user_ego_cycle", status="rejected"):
    await ego_crud.create_proposal(
        db,
        id=pid,
        action_type="content_publishing",
        action_category="marketing",
        content="Republish the launch post under a de-identified handle.",
        confidence=0.8,
        ego_source=ego_source,
    )
    await ego_crud.resolve_proposal(db, pid, status=status, user_response="r")
    return await ego_crud.get_proposal(db, pid)


async def _decisions(db, ego_target="user_ego"):
    rows, total = await ego_crud.list_active_decisions(db, ego_target=ego_target)
    return rows, total


# ── Decision capture matrix ────────────────────────────────────────────────


async def test_reject_with_reason_creates_decision(db):
    prop = await _proposal(db)
    await handle_proposal_resolution(
        db, prop, "rejected", reason="OBA is settled; publish under my name.",
        source="dashboard",
    )
    rows, total = await _decisions(db)
    assert total == 1
    assert rows[0]["kind"] == "decision"
    assert rows[0]["content"].startswith("[content_publishing/marketing]")
    assert "OBA is settled" in rows[0]["content"]
    assert rows[0]["source_proposal_id"] == "p1"


async def test_repeat_ruling_reaffirms_not_duplicates(db):
    prop = await _proposal(db)
    for _ in range(3):
        await handle_proposal_resolution(
            db, prop, "rejected", reason="Same ruling again.", source="mcp",
        )
    rows, total = await _decisions(db)
    assert total == 1
    assert rows[0]["reaffirm_count"] == 2
    assert rows[0]["last_reaffirmed_at"]


async def test_standing_rule_overrides_verbatim_reason(db):
    prop = await _proposal(db)
    await handle_proposal_resolution(
        db, prop, "rejected", reason="long rambling explanation …",
        standing_rule="De-identification only on strategic merits, never compliance.",
        source="mcp",
    )
    rows, _ = await _decisions(db)
    assert "strategic merits" in rows[0]["content"]
    assert "rambling" not in rows[0]["content"]


async def test_no_decision_on_approve(db):
    prop = await _proposal(db, status="approved")
    await handle_proposal_resolution(
        db, prop, "approved", reason="sure", source="telegram",
    )
    assert (await _decisions(db))[1] == 0


async def test_no_decision_without_reason(db):
    prop = await _proposal(db)
    await handle_proposal_resolution(db, prop, "rejected", source="telegram")
    assert (await _decisions(db))[1] == 0


async def test_no_decision_when_one_off(db):
    prop = await _proposal(db)
    await handle_proposal_resolution(
        db, prop, "rejected", reason="not right now", one_off=True,
        source="mcp",
    )
    assert (await _decisions(db))[1] == 0


async def test_genesis_ego_proposal_targets_genesis_decisions(db):
    prop = await _proposal(db, ego_source="genesis_ego_cycle")
    await handle_proposal_resolution(
        db, prop, "rejected", reason="Never restart the DB mid-backup.",
        source="telegram",
    )
    assert (await _decisions(db, "user_ego"))[1] == 0
    assert (await _decisions(db, "genesis_ego"))[1] == 1


# ── Artifact parity ────────────────────────────────────────────────────────


async def test_hook_writes_journal_and_correction_memory(db):
    prop = await _proposal(db)
    store = AsyncMock()
    await handle_proposal_resolution(
        db, prop, "rejected", reason="No.", source="dashboard",
        memory_store=store,
    )
    store.store.assert_awaited_once()
    kwargs = store.store.await_args.kwargs
    assert "ego_correction" in kwargs["tags"]
    assert kwargs["source"] == "ego_correction"


async def test_hook_failure_isolation(db):
    """A broken memory store never blocks decision capture or the hook."""
    prop = await _proposal(db)
    store = AsyncMock()
    store.store.side_effect = RuntimeError("qdrant down")
    await handle_proposal_resolution(
        db, prop, "rejected", reason="Still captured.", source="dashboard",
        memory_store=store,
    )
    assert (await _decisions(db))[1] == 1


# ── Decision lifecycle guards ──────────────────────────────────────────────


async def test_ego_cannot_resolve_a_decision(db):
    did = await ego_crud.create_decision(db, content="[x/y] a ruling")
    assert await ego_crud.resolve_directive(db, did, status="completed") is False
    rows, _ = await _decisions(db)
    assert rows[0]["id"] == did  # still active


async def test_supersede_is_the_only_retire_path(db):
    did = await ego_crud.create_decision(db, content="[x/y] a ruling")
    assert await ego_crud.supersede_decision(db, did, resolution="user revoked")
    assert (await _decisions(db))[1] == 0
    row = await db.execute_fetchall(
        "SELECT status, resolution FROM ego_directives WHERE id = ?", (did,),
    )
    assert row[0]["status"] == "cancelled"
    assert row[0]["resolution"].startswith("superseded")


async def test_directives_listing_excludes_decisions(db):
    await ego_crud.create_directive(db, content="a plain directive")
    await ego_crud.create_decision(db, content="[x/y] a ruling")
    dirs = await ego_crud.list_active_directives(db)
    assert [d["kind"] for d in dirs] == ["directive"]


# ── Telegram workflow integration (the real resolve path) ─────────────────


async def test_workflow_resolution_captures_decision(db):
    from genesis.ego.proposals import ProposalWorkflow

    workflow = ProposalWorkflow(db=db)
    await ego_crud.create_proposal(
        db, id="wf1", action_type="content_publishing",
        action_category="marketing", content="proposal text",
        batch_id="b1", ego_source="user_ego_cycle",
    )
    await workflow.resolve_proposals("b1", {1: ("rejected", "Ruled out; settled.")})
    rows, total = await _decisions(db)
    assert total == 1
    assert rows[0]["source_proposal_id"] == "wf1"


# ── Context rendering ──────────────────────────────────────────────────────


async def test_settled_decisions_section_renders_and_is_always_on(db):
    from genesis.ego.focus import _ALL_SECTIONS, _ALWAYS_SECTIONS
    from genesis.ego.user_context import UserEgoContextBuilder

    assert "settled_decisions" in _ALL_SECTIONS
    assert "settled_decisions" in _ALWAYS_SECTIONS

    await ego_crud.create_decision(
        db, content="[content_publishing/marketing] Publish under the user's own name.",
    )
    builder = UserEgoContextBuilder(db=db, health_data={}, capabilities=[])
    result = await builder.build()
    assert "## Settled Decisions" in result
    assert "Publish under the user's own name." in result
    assert "Do not re-propose" in result


async def test_settled_decisions_empty_state_renders_nothing(db):
    from genesis.ego.user_context import UserEgoContextBuilder

    builder = UserEgoContextBuilder(db=db, health_data={}, capabilities=[])
    result = await builder.build()
    assert "## Settled Decisions" not in result


async def test_overflow_marker_when_more_than_cap(db):
    from genesis.ego.user_context import UserEgoContextBuilder

    for i in range(9):
        await ego_crud.create_decision(db, content=f"[t{i}/c] ruling {i}")
    builder = UserEgoContextBuilder(db=db, health_data={}, capabilities=[])
    result = await builder.build()
    assert "+2 older active rulings not shown" in result


# ── Migration 0066 ─────────────────────────────────────────────────────────


async def test_migration_0066_idempotent(tmp_path):
    m66 = importlib.import_module(
        "genesis.db.migrations.0066_ego_directive_decisions"
    )
    conn = await aiosqlite.connect(str(tmp_path / "m.db"))
    try:
        # pre-0066 shape
        await conn.execute(
            """CREATE TABLE ego_directives (
                id TEXT PRIMARY KEY, content TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'normal',
                source TEXT NOT NULL DEFAULT 'user',
                ego_target TEXT NOT NULL DEFAULT 'user_ego',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT
            )"""
        )
        await conn.execute(
            "INSERT INTO ego_directives (id, content, created_at) "
            "VALUES ('d1', 'old directive', '2026-01-01T00:00:00+00:00')"
        )
        await m66.up(conn)
        await conn.commit()
        await m66.up(conn)  # idempotent
        await conn.commit()
        cur = await conn.execute("SELECT kind, reaffirm_count FROM ego_directives")
        row = await cur.fetchone()
        assert tuple(row) == ("directive", 0)
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_ego_directives_kind_status'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()


# ── decision_prefix helper ─────────────────────────────────────────────────


def test_decision_prefix_defaults():
    assert decision_prefix({}) == "[unknown/general]"
    assert decision_prefix(
        {"action_type": "a", "action_category": "b"}
    ) == "[a/b]"
