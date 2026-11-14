"""Ordinary memory uses the existing P7 decision at its admitted read stages."""

import json

import pytest

from tests.test_mcp.test_shared_service_settings import checkout as checkout


@pytest.fixture
def memory_checkout(checkout):
    root, home, reader, config, health_spec = checkout
    # Test-owned ceiling only. Production MAX_STAGE and registrations stay off.
    reader.write_text(reader.read_text() + '\nMAX_STAGE = "health-reads"\n')
    spec = {**health_spec, "unit": "genesis-mcp-memory.service"}
    config.write_text(json.dumps({
        "stage": "memory-floor",
        "adapter": {"argv": ["{python}", "{root}/scripts/genesis_mcp_server.py", "--server", "{server}"]},
        "servers": {"memory": spec},
    }))
    return root, home, reader, config, spec


@pytest.mark.parametrize("stage", ["memory-floor", "health-reads"])
def test_memory_selection_uses_the_real_rollout_snapshot(memory_checkout, stage):
    from genesis.mcp.shared_settings import load_local_settings

    root, home, reader, config, spec = memory_checkout
    data = json.loads(config.read_text())
    data["stage"] = stage
    config.write_text(json.dumps(data))
    stdio = ["/fixture/python", str(root / "scripts/genesis_mcp_server.py"), "--server", "memory"]
    seen = []

    def ready(unit):
        seen.append(unit)
        return True, "running"

    selected = load_local_settings(root, "memory", {"GENESIS_HOME": str(home)}, stdio, ready=ready)
    assert selected is not None, "ordinary memory was not selected at its admitted read stage"
    assert selected.stage == stage and selected.settings.http.service == "memory"
    assert selected.settings.http.url == spec["url"]
    assert selected.settings.unit == spec["unit"] and seen == [spec["unit"]]


@pytest.mark.parametrize("change", [
    "ceiling-off", "kill", "label-codex", "label-external", "label-validator", "label-unknown",
    "not-ready", "no-adapter", "custom-adapter", "extra-argv", "stage-off", "stage-writes",
    "environment-stage-off", "other-service",
])
def test_ineligible_memory_spawn_does_not_require_shared_limits(memory_checkout, change):
    from genesis.mcp.shared_settings import load_local_settings

    root, home, reader, config, spec = memory_checkout
    env = {"GENESIS_HOME": str(home)}
    stdio = ["/fixture/python", str(root / "scripts/genesis_mcp_server.py"), "--server", "memory"]
    data = json.loads(config.read_text())
    service = "memory"
    if change == "ceiling-off":
        reader.write_text(reader.read_text() + '\nMAX_STAGE = "off"\n')
    elif change == "kill":
        env["GENESIS_MCP_SHARED_DISABLED"] = "1"
    elif change.startswith("label-"):
        env["GENESIS_CALLER_PROFILE"] = change.removeprefix("label-")
    elif change == "no-adapter":
        data["adapter"]["argv"] = []
    elif change == "custom-adapter":
        data["adapter"]["argv"] = ["{python}", "{root}/custom.py", "{stage}"]
    elif change == "extra-argv":
        stdio += ["--external-client"]
    elif change == "stage-off":
        data["stage"] = "off"
    elif change == "stage-writes":
        reader.write_text(reader.read_text() + '\nMAX_STAGE = "writes"\n')
        data["stage"] = "writes"
    elif change == "environment-stage-off":
        env["GENESIS_MCP_SHARED_STAGE"] = "off"
    elif change == "other-service":
        service, stdio[-1] = "recon", "recon"
    data["servers"]["memory"].pop("db_units")
    config.write_text(json.dumps(data))
    assert load_local_settings(root, service, env, stdio, ready=lambda unit: (change != "not-ready", "fixture")) is None


@pytest.mark.parametrize("missing", ["memory", "db_units"])
def test_selected_memory_missing_configuration_refuses(memory_checkout, missing):
    from genesis.mcp.shared_settings import load_local_settings

    root, home, reader, config, spec = memory_checkout
    data = json.loads(config.read_text())
    if missing == "memory":
        del data["servers"]["memory"]
    else:
        del data["servers"]["memory"][missing]
    config.write_text(json.dumps(data))
    stdio = ["/fixture/python", str(root / "scripts/genesis_mcp_server.py"), "--server", "memory"]
    with pytest.raises(ValueError, match="Invalid local MCP configuration") as error:
        load_local_settings(root, "memory", {"GENESIS_HOME": str(home)}, stdio, ready=lambda unit: (True, "running"))
    assert str(root) not in str(error.value)
