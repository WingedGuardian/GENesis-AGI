"""Tests for recon_schedule MCP tool."""

import pytest
import yaml

from genesis.mcp import recon_mcp
from genesis.mcp.recon_mcp import mcp


@pytest.fixture
def schedule_file(tmp_path, monkeypatch):
    path = tmp_path / "recon_schedules.yaml"
    path.write_text(yaml.safe_dump({
        "schedules": {
            "email_recon": {"description": "Scan email", "cron": "0 5 * * *", "enabled": True},
            "web_monitoring": {"description": "Web check", "cron": "0 6 * * 5", "enabled": True},
        }
    }))
    monkeypatch.setattr(recon_mcp, "_SCHEDULES_PATH", path)
    return path


@pytest.fixture
async def tools(schedule_file):
    return await mcp.get_tools()


async def test_view_schedule(tools):
    result = await tools["recon_schedule"].fn(job_type="email_recon")
    assert result["cron"] == "0 5 * * *"
    assert result["enabled"] is True


async def test_update_schedule(tools, schedule_file):
    result = await tools["recon_schedule"].fn(job_type="email_recon", new_schedule="0 4 * * *")
    assert result["updated"] is True
    assert result["cron"] == "0 4 * * *"

    # Verify persistence
    data = yaml.safe_load(schedule_file.read_text())
    assert data["schedules"]["email_recon"]["cron"] == "0 4 * * *"


async def test_unknown_job_type(tools):
    result = await tools["recon_schedule"].fn(job_type="nonexistent")
    assert "error" in result
    assert "nonexistent" in result["error"]
