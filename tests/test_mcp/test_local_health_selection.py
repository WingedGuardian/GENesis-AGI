"""Fresh P7 selection for the ordinary health frontend; no handoff state."""

import json
from dataclasses import FrozenInstanceError

import pytest

from tests.test_mcp.test_shared_service_settings import checkout as checkout


def configured(checkout):
    root, home, reader, config, spec = checkout
    # Test-owned trusted program only. The shipped code ceiling stays off.
    reader.write_text(reader.read_text() + '\nMAX_STAGE = "health-reads"\n')
    config.write_text(json.dumps({
        "stage": "health-reads",
        "adapter": {"argv": ["{python}", "{root}/scripts/genesis_mcp_server.py", "--server", "{server}"]},
        "servers": {"health": spec},
    }))
    stdio = ["/fixture/python", str(root / "scripts/genesis_mcp_server.py"), "--server", "health"]
    return root, {"GENESIS_HOME": str(home)}, stdio


def select(root, env, stdio, *, ready=lambda unit: (True, "running"), service="health"):
    from genesis.mcp import shared_settings

    function = getattr(shared_settings, "load_local_settings", None)
    assert callable(function), "ordinary health has no coherent local selection reader"
    return function(root, service, env, stdio, ready=ready)


def test_selected_settings_and_stage_share_the_actual_decision_snapshot(checkout):
    root, env, stdio = configured(checkout)
    seen = []

    def ready(unit):
        seen.append(unit)
        # A later overlay must not change the endpoint/limits in this selection.
        overlay = checkout[1] / "config/mcp_shared.json"
        overlay.parent.mkdir(parents=True)
        overlay.write_text(json.dumps({"servers": {"health": {"url": "http://127.0.0.1:9124/mcp"}}}))
        return True, "running"

    selected = select(root, env, stdio, ready=ready)
    assert seen == ["genesis-mcp-health.service"]
    assert selected.stage == "health-reads"
    assert selected.settings.http.url == "http://127.0.0.1:9123/mcp"
    assert selected.settings.http.db_units == 2
    assert select(root, env, stdio).settings.http.url == "http://127.0.0.1:9124/mcp"
    with pytest.raises(FrozenInstanceError):
        selected.stage = "writes"


@pytest.mark.parametrize("change", [
    "ceiling-off", "kill", "label-codex", "label-validator", "label-unknown",
    "not-ready", "no-adapter", "custom-adapter", "extra-argv", "other-service",
    "stage-off", "stage-memory", "stage-writes", "environment-stage-off",
])
def test_ineligible_spawns_stay_ordinary_before_parsing_shared_limits(checkout, change):
    root, env, stdio = configured(checkout)
    reader, config = checkout[2:4]
    data = json.loads(config.read_text())
    ready_status = change != "not-ready"

    def ready(unit):
        return ready_status, "running" if ready_status else "inactive"

    service = "health"
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
    elif change == "other-service":
        service, stdio[-1] = "outreach", "outreach"
    elif change == "stage-off":
        data["stage"] = "off"
    elif change == "stage-memory":
        data["stage"] = "memory-floor"
    elif change == "stage-writes":
        reader.write_text(reader.read_text() + '\nMAX_STAGE = "writes"\n')
        data["stage"] = "writes"
    elif change == "environment-stage-off":
        env["GENESIS_MCP_SHARED_STAGE"] = "off"
    # An ordinary install need not supply the shared-specific limit population.
    data["servers"]["health"].pop("db_units")
    config.write_text(json.dumps(data))
    assert select(root, env, stdio, ready=ready, service=service) is None


def test_selected_bad_limits_refuse_without_values_or_paths(checkout):
    root, env, stdio = configured(checkout)
    config = checkout[3]
    data = json.loads(config.read_text())
    data["servers"]["health"]["url"] = "http://fixture-sensitive-value@127.0.0.1:9123/mcp"
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError) as error:
        select(root, env, stdio)
    assert str(root) not in str(error.value) and "fixture-sensitive-value" not in str(error.value)


def test_rollout_program_namespace_is_released_after_local_selection(checkout):
    import sys

    root, env, stdio = configured(checkout)
    before = set(sys.modules)
    assert select(root, env, stdio) is not None
    assert not {name for name in set(sys.modules) - before if name.startswith("_genesis_shared_rollout_")}
