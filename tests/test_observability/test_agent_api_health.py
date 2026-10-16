"""A current health observation must belong to the running primary server."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.observability.agent_api_health import from_status_file, sample

NOW = datetime(2026, 10, 16, 20, tzinfo=UTC)


def observation():
    return dict(status="healthy", reasons=[], checked_at=NOW.isoformat(), pid=12345,
                runtime_ready=True, task_service_ready=True, source="live")


def server():
    return {"bridge": dict(service_unit="genesis-server.service", active_state="active",
                           main_pid="12345")}


@pytest.mark.parametrize("age,expected", [(-1, "unknown"), (0, "healthy"),
                                         (90, "healthy"), (91, "unknown")])
def test_sample_age(age, expected):
    value = observation()
    value["checked_at"] = (NOW - timedelta(seconds=age)).isoformat()
    result = from_status_file(value, server(), now=NOW)
    assert result["status"] == expected
    if expected == "healthy":
        assert result["checked_at"] == value["checked_at"]
        assert result["source"] == "status_file"


@pytest.mark.parametrize("fault", ["missing", "malformed", "naive", "pid", "dead", "bridge"])
def test_invalid_or_foreign_observations(fault):
    value, services = observation(), server()
    if fault == "missing":
        value = None
    elif fault == "malformed":
        value["checked_at"] = "yesterday"
    elif fault == "naive":
        value["checked_at"] = NOW.replace(tzinfo=None).isoformat()
    elif fault == "pid":
        services["bridge"]["main_pid"] = "54321"
    elif fault == "dead":
        services["bridge"]["active_state"] = "inactive"
    else:
        services["bridge"]["service_unit"] = "genesis-bridge.service"
    assert from_status_file(value, services, now=NOW)["status"] == "unknown"


async def test_writer_reader_and_late_binding(tmp_path, monkeypatch):
    from genesis.mcp.standalone_health import StandaloneHealthDataService
    from genesis.resilience.state import ResilienceStateMachine
    from genesis.resilience.status_writer import StatusFileWriter

    runtime = SimpleNamespace(job_health={})
    path = tmp_path / "status.json"
    writer = StatusFileWriter(state_machine=ResilienceStateMachine(), runtime=runtime, path=str(path))
    await writer.write()
    assert json.loads(path.read_text())["agent_api"]["status"] == "unknown"
    value = observation()
    value["checked_at"] = datetime.now(UTC).isoformat()
    runtime._agent_api_readiness = AsyncMock(return_value=value)
    await writer.write()
    monkeypatch.setattr("genesis.observability.service_status.collect_service_status", server)
    result = await StandaloneHealthDataService(status_path=path).snapshot()
    assert result["agent_api"]["status"] == "healthy"
    assert result["agent_api"]["checked_at"] == value["checked_at"]


async def test_live_reader_can_activate_without_restart():
    runtime = SimpleNamespace()
    assert (await sample(runtime))["status"] == "unknown"
    runtime._agent_api_readiness = AsyncMock(return_value=observation())
    assert (await sample(runtime))["status"] == "healthy"
