"""Tests for genesis.autonomy.approval.ApprovalManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def manager(db):
    return ApprovalManager(db=db)


async def _create_request(mgr: ApprovalManager, **kwargs) -> str:
    defaults = {
        "action_type": "test_action",
        "action_class": "reversible",
        "description": "test description",
    }
    defaults.update(kwargs)
    return await mgr.request_approval(**defaults)


# ------------------------------------------------------------------
# Basic lifecycle
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_creates_entry(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    assert req_id
    row = await mgr.get_by_id(req_id)
    assert row is not None
    assert row["id"] == req_id


@pytest.mark.asyncio
async def test_request_pending_status(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_approved(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    ok = await mgr.resolve(req_id, status="approved")
    assert ok is True
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "approved"


@pytest.mark.asyncio
async def test_resolve_rejected(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    ok = await mgr.resolve(req_id, status="rejected")
    assert ok is True
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "rejected"


@pytest.mark.asyncio
async def test_resolve_only_pending(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    assert await mgr.resolve(req_id, status="approved") is True
    assert await mgr.resolve(req_id, status="rejected") is False
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "approved"


@pytest.mark.asyncio
async def test_cancel_sets_cancelled(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr)
    ok = await mgr.cancel(req_id)
    assert ok is True
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_resolve_nonexistent_returns_false(db):
    mgr = ApprovalManager(db=db)
    ok = await mgr.resolve("nonexistent-id", status="approved")
    assert ok is False


@pytest.mark.asyncio
async def test_expire_timed_out(db):
    mgr = ApprovalManager(db=db)
    req_id = await _create_request(mgr, timeout_seconds=3600)
    # Manually set timeout_at to the past
    await db.execute(
        "UPDATE approval_requests SET timeout_at = '2020-01-01T00:00:00' WHERE id = ?",
        (req_id,),
    )
    await db.commit()
    expired = await mgr.expire_timed_out()
    assert expired == 1
    row = await mgr.get_by_id(req_id)
    assert row["status"] == "expired"


@pytest.mark.asyncio
async def test_no_timeout_never_expires(db):
    mgr = ApprovalManager(db=db)
    await _create_request(mgr, timeout_seconds=None)
    expired = await mgr.expire_timed_out()
    assert expired == 0


@pytest.mark.asyncio
async def test_get_pending_returns_only_pending(db):
    mgr = ApprovalManager(db=db)
    id1 = await _create_request(mgr)
    id2 = await _create_request(mgr)
    await mgr.resolve(id1, status="approved")
    pending = await mgr.get_pending()
    pending_ids = [r["id"] for r in pending]
    assert id2 in pending_ids
    assert id1 not in pending_ids


@pytest.mark.asyncio
async def test_classifier_timeout_lookup(db):
    classifier = MagicMock()
    classifier.get_timeout.return_value = 120
    mgr = ApprovalManager(db=db, classifier=classifier)
    req_id = await _create_request(mgr)
    classifier.get_timeout.assert_called_once_with("test_action")
    row = await mgr.get_by_id(req_id)
    assert row["timeout_at"] is not None


@pytest.mark.asyncio
async def test_event_emitted_on_request(db):
    event_bus = AsyncMock()
    mgr = ApprovalManager(db=db, event_bus=event_bus)
    await _create_request(mgr)
    event_bus.emit.assert_called_once()


# ------------------------------------------------------------------
# Voice-gated approval resolution (AutonomousCliApprovalGate)
# ------------------------------------------------------------------


def _voice_gate(mgr):
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    return AutonomousCliApprovalGate(runtime=MagicMock(), approval_manager=mgr)


def test_build_greenlight_is_voice_gated():
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    assert "build_greenlight" in AutonomousCliApprovalGate._VOICE_GATED_TYPES


def test_format_message_build_greenlight_card():
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    msg = AutonomousCliApprovalGate._format_message(
        request_id="req-1",
        action_label="Build: Widget Skill [abc12345]",
        invocation=None,
        api_error=None,
        action_type="build_greenlight",
        extra_context={
            "title": "Widget Skill",
            "steps_count": 3,
            "intended_paths": ["src/genesis/skills/widget/"],
            "plan_path": "/home/u/.genesis/plans/build-abc12345-2026-07-07.md",
        },
    )
    assert "Build Greenlight" in msg
    assert "Widget Skill" in msg
    assert "draft PR" in msg
    assert "3 step(s)" in msg
    assert "src/genesis/skills/widget/" in msg
    # Must NOT fall through to the misleading generic CLI-fallback copy.
    assert "autonomous Claude Code fallback" not in msg


@pytest.mark.asyncio
async def test_resolve_pending_voice_single(db):
    mgr = ApprovalManager(db=db)
    rid = await _create_request(
        mgr, action_type="sentinel_dispatch", description="investigate the thing",
    )
    gate = _voice_gate(mgr)
    result = await gate.resolve_pending_voice(
        decision="approved", resolved_by="voice:s2s",
    )
    assert result["status"] == "resolved"
    assert result["request_id"] == rid
    assert result["label"] == "investigate the thing"
    assert (await mgr.get_by_id(rid))["status"] == "approved"


@pytest.mark.asyncio
async def test_resolve_pending_voice_by_id(db):
    mgr = ApprovalManager(db=db)
    rid1 = await _create_request(
        mgr, action_type="sentinel_dispatch", description="a",
    )
    rid2 = await _create_request(
        mgr, action_type="autonomous_cli_fallback", description="b",
    )
    gate = _voice_gate(mgr)
    result = await gate.resolve_pending_voice(
        decision="approved", resolved_by="voice:s2s", request_id=rid2,
    )
    assert result["status"] == "resolved"
    assert result["request_id"] == rid2
    assert (await mgr.get_by_id(rid1))["status"] == "pending"
    assert (await mgr.get_by_id(rid2))["status"] == "approved"


@pytest.mark.asyncio
async def test_resolve_pending_voice_ambiguous_resolves_nothing(db):
    mgr = ApprovalManager(db=db)
    rid1 = await _create_request(
        mgr, action_type="sentinel_dispatch", description="a",
    )
    rid2 = await _create_request(
        mgr, action_type="sentinel_action", description="b",
    )
    gate = _voice_gate(mgr)
    result = await gate.resolve_pending_voice(
        decision="approved", resolved_by="voice:s2s",
    )
    assert result["status"] == "ambiguous"
    assert {c["id"] for c in result["candidates"]} == {rid1, rid2}
    # Neither was resolved — refuses to guess.
    assert (await mgr.get_by_id(rid1))["status"] == "pending"
    assert (await mgr.get_by_id(rid2))["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_pending_voice_none(db):
    mgr = ApprovalManager(db=db)
    gate = _voice_gate(mgr)
    result = await gate.resolve_pending_voice(
        decision="approved", resolved_by="voice:s2s",
    )
    assert result["status"] == "none"


@pytest.mark.asyncio
async def test_resolve_pending_voice_not_found(db):
    mgr = ApprovalManager(db=db)
    await _create_request(
        mgr, action_type="sentinel_dispatch", description="a",
    )
    gate = _voice_gate(mgr)
    result = await gate.resolve_pending_voice(
        decision="approved", resolved_by="voice:s2s", request_id="does-not-exist",
    )
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_resolve_pending_voice_invalid_decision(db):
    mgr = ApprovalManager(db=db)
    gate = _voice_gate(mgr)
    result = await gate.resolve_pending_voice(
        decision="maybe", resolved_by="voice:s2s",
    )
    assert result["status"] == "invalid_decision"


@pytest.mark.asyncio
async def test_pending_voice_actions_excludes_non_voice_types(db):
    mgr = ApprovalManager(db=db)
    await _create_request(
        mgr, action_type="sentinel_dispatch", description="voice one",
    )
    await _create_request(
        mgr, action_type="ego_proposal", description="not voice",
    )
    gate = _voice_gate(mgr)
    actions = await gate.pending_voice_actions()
    assert len(actions) == 1
    assert actions[0]["action_type"] == "sentinel_dispatch"
    assert actions[0]["label"] == "voice one"
