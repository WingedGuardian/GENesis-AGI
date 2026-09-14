"""The ``adapter:`` selector must fail CLOSED, in BOTH loaders.

A silent fallback to the plain ExternalProgramAdapter hands back a module that
registers, reports healthy, appears in ``module_list`` and cannot do the one
thing its config asked for — the least debuggable failure available, because
every surface a person would check says the module is fine.

Two loaders read these configs (``runtime/init/modules.py`` for the runtime and
``mcp/health/module_ops.py`` for the MCP tools), and an invariant that holds in
one of two is not an invariant.
"""
from __future__ import annotations

from genesis.modules.endpoint.adapter import WindowsEndpointAdapter
from genesis.modules.external.adapter import ExternalProgramAdapter
from genesis.runtime.init.modules import _load_external_module

_ENDPOINT_BLOCK = {
    "state_dir": r"C:\Users\X\AppData\Local\Genesis\desktop",
    "mission_command": r"powershell -File C:\scripts\genesis-act.ps1",
    "machine_id": "GUID-1",
}


def _data(**over):
    d = {
        "name": "Test Endpoint",
        "type": "external",
        "ipc": {"method": "ssh", "ssh_host": "u@100.64.0.1"},
        "endpoint": dict(_ENDPOINT_BLOCK),
    }
    d.update(over)
    return d


def test_no_adapter_field_yields_the_plain_external_adapter():
    """Every existing config omits `adapter:` and must be unaffected."""
    mod = _load_external_module(
        {"name": "Plain", "ipc": {"method": "http", "url": "http://x"}}, "p.yaml"
    )
    assert type(mod) is ExternalProgramAdapter


def test_known_adapter_yields_the_subclass():
    mod = _load_external_module(_data(adapter="windows-endpoint"), "e.yaml")
    assert isinstance(mod, WindowsEndpointAdapter)


def test_the_generic_name_is_not_accepted():
    """Named by DIALECT. `endpoint` must not quietly mean `windows-endpoint`."""
    assert _load_external_module(_data(adapter="endpoint"), "generic.yaml") is None


def test_unknown_adapter_is_refused_rather_than_falling_back(caplog):
    mod = _load_external_module(_data(adapter="windows-endpiont"), "typo.yaml")
    assert mod is None, "a typo'd adapter must not silently load as a plain module"
    assert "unknown adapter" in caplog.text


def test_a_construction_failure_refuses_the_module(caplog):
    d = _data(adapter="windows-endpoint")
    del d["endpoint"]
    assert _load_external_module(d, "broken.yaml") is None
    assert "failed to construct" in caplog.text


def _mcp_adapters(tmp_path, monkeypatch, data):
    import yaml

    from genesis.mcp.health import module_ops

    (tmp_path / "ep.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setattr(module_ops, "_MODULES_DIR", tmp_path)
    monkeypatch.setattr(module_ops, "_LOCAL_MODULES_DIR", tmp_path / "nope")
    # None, not {}: _get_adapters returns early when _adapters is not None, so
    # an empty dict would make the assertions below pass without ever scanning.
    monkeypatch.setattr(module_ops, "_adapters", None)
    return module_ops._get_adapters()


def test_the_mcp_loader_makes_the_SAME_choice_as_the_runtime_loader(tmp_path, monkeypatch):
    """Both loaders share one factory, so neither can drift from the other.

    Previously this path built a plain ExternalProgramAdapter for every external
    config — handing ``module_call`` exactly the object the runtime loader
    refuses to construct. Skipping instead would have been safe but would omit
    the module from ``module_list`` entirely, reading as "not configured".
    """
    adapters = _mcp_adapters(tmp_path, monkeypatch, _data(adapter="windows-endpoint"))
    assert isinstance(adapters.get("Test Endpoint"), WindowsEndpointAdapter)


def test_the_mcp_loader_also_refuses_an_unknown_adapter(tmp_path, monkeypatch, caplog):
    adapters = _mcp_adapters(tmp_path, monkeypatch, _data(adapter="windows-endpiont"))
    assert "Test Endpoint" not in adapters
    assert "unknown adapter" in caplog.text


# ── real YAML, not a hand-built dict ────────────────────────────────────────
def test_the_shipped_template_parses_and_builds_a_working_adapter():
    """The dict-based tests above cannot catch a YAML-level defect.

    Notably: a Windows path in a DOUBLE-quoted YAML scalar makes the whole file
    fail to parse, because a backslash starts an escape sequence there.
    """
    import yaml

    from genesis.env import repo_root

    data = yaml.safe_load((repo_root() / "config/modules/endpoint.yaml.template").read_text())

    assert data["adapter"] == "windows-endpoint"
    assert data["enabled"] is False, "the template must ship disabled"
    assert "health_check" not in data, (
        "a health_check block activates the inherited `claude --version` probe, "
        "which pins a Windows endpoint permanently unhealthy"
    )
    assert "\\" in data["endpoint"]["state_dir"], "backslashes must survive YAML parsing"

    data["endpoint"]["machine_id"] = "REAL-GUID"
    data["ipc"]["ssh_host"] = "u@192.168.1.5"
    mod = _load_external_module(data, "endpoint.yaml.template")
    assert isinstance(mod, WindowsEndpointAdapter)
    assert mod.payload_budget() > 1000


def test_a_double_quoted_windows_path_is_a_yaml_error_not_a_silent_mangle():
    """Documents the trap the template's comment warns about."""
    import pytest
    import yaml

    with pytest.raises(yaml.YAMLError):
        yaml.safe_load('state_dir: "C:\\Users\\X\\AppData"')
