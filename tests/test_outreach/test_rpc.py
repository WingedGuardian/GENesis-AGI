"""Tests for the shared in-process RPC implementations (outreach/rpc.py) used by
both the MCP tools (in-server) and the dashboard bridge routes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.outreach.rpc import grow_via_pipeline, send_and_wait_via_pipeline


@pytest.mark.asyncio
async def test_send_and_wait_via_pipeline_invalid_category():
    result = await send_and_wait_via_pipeline(
        AsyncMock(), message="hi", category="nope", channel="telegram", timeout_s=5,
    )
    assert "invalid category" in result["error"].lower()


@pytest.mark.asyncio
async def test_send_and_wait_via_pipeline_delivers():
    res = MagicMock()
    res.outreach_id = "o-1"
    res.status.value = "delivered"
    pipe = AsyncMock()
    pipe.submit_and_wait = AsyncMock(return_value=(res, "approved"))

    out = await send_and_wait_via_pipeline(
        pipe, message="approve?", category="blocker", channel="telegram", timeout_s=30,
    )
    assert out == {"outreach_id": "o-1", "status": "delivered",
                   "reply": "approved", "timed_out": False}
    # timeout threaded through as a float
    assert pipe.submit_and_wait.call_args.kwargs["timeout_s"] == 30.0


@pytest.mark.asyncio
async def test_grow_via_pipeline_no_remote_returns_clean_error():
    with patch("genesis.observability.health._load_guardian_remote_from_config",
               return_value=None):
        out = await grow_via_pipeline(
            AsyncMock(), kind="disk", disk="scsi1", gib=1, mib=0, timeout_s=5,
        )
    assert out["ok"] is False
    assert "guardian remote not configured" in out["error"]


@pytest.mark.asyncio
async def test_grow_via_pipeline_disk_asks_then_executes():
    remote = MagicMock()
    coord = AsyncMock(return_value={"ok": True, "stage": "executed"})
    with patch("genesis.observability.health._load_guardian_remote_from_config",
               return_value=remote), \
         patch("genesis.guardian.provisioning.container.coordinate_grow_disk", coord):
        out = await grow_via_pipeline(
            AsyncMock(), kind="disk", disk="scsi1", gib=2, mib=0, timeout_s=5,
        )
    assert out == {"ok": True, "stage": "executed"}
    # coordinate_grow_disk(remote, _ask, disk=..., add_gib=...)
    assert coord.call_args.args[0] is remote
    assert coord.call_args.kwargs == {"disk": "scsi1", "add_gib": 2}


@pytest.mark.asyncio
async def test_grow_via_pipeline_invalid_kind():
    with patch("genesis.observability.health._load_guardian_remote_from_config",
               return_value=MagicMock()):
        out = await grow_via_pipeline(
            AsyncMock(), kind="bogus", disk="scsi1", gib=1, mib=0, timeout_s=5,
        )
    assert out["ok"] is False
    assert "invalid kind" in out["error"]


@pytest.mark.asyncio
async def test_grow_via_pipeline_root_asks_then_executes():
    remote = MagicMock()
    coord = AsyncMock(return_value={"ok": True, "verified": True})
    with patch("genesis.observability.health._load_guardian_remote_from_config",
               return_value=remote), \
         patch("genesis.guardian.provisioning.container.coordinate_grow_root", coord):
        out = await grow_via_pipeline(
            AsyncMock(), kind="root", disk="scsi1", gib=40, mib=0, timeout_s=5,
        )
    assert out["ok"] is True
    assert coord.call_args.kwargs == {"new_gb": 40}


@pytest.mark.asyncio
async def test_grow_via_pipeline_limits_passes_mem_and_cpu():
    remote = MagicMock()
    coord = AsyncMock(return_value={"ok": True})
    with patch("genesis.observability.health._load_guardian_remote_from_config",
               return_value=remote), \
         patch("genesis.guardian.provisioning.container.coordinate_set_container_limits", coord):
        out = await grow_via_pipeline(
            AsyncMock(), kind="limits", disk="scsi1", gib=0, mib=20480, cpu=4, timeout_s=5,
        )
    assert out["ok"] is True
    assert coord.call_args.kwargs == {"mem_mib": 20480, "cpu": 4}
