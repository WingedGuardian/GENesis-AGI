"""One stdlib configuration reader for script, captured deploy and service callers."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/lib/mcp_rollout.py"
READER = REPO / "src/genesis/mcp/shared_config.py"
LIB = REPO / "scripts/lib/shared_mcp_units.sh"


def rollout_module():
    spec = importlib.util.spec_from_file_location("shared_config_rollout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rollout_reexports_the_same_reader_and_error():
    from genesis.mcp import shared_config

    rollout = rollout_module()
    assert rollout.load_config is shared_config.load_config
    assert rollout.overlay_path is shared_config.overlay_path
    assert rollout.RolloutError is shared_config.SharedConfigError


def test_loader_preserves_per_key_not_recursive_overlay_merge(tmp_path):
    from genesis.mcp.shared_config import load_config

    root, home = tmp_path / "checkout", tmp_path / "home"
    for path in (root, home):
        (path / "config").mkdir(parents=True)
    (root / "config/mcp_shared.json").write_text(json.dumps({
        "stage": "off", "adapter": {"argv": ["fixture"], "extra": 1},
        "servers": {"memory": {"url": "http://127.0.0.1:8101/mcp", "service": {"a": 1}}},
    }))
    (home / "config/mcp_shared.json").write_text(json.dumps({
        "stage": "writes", "adapter": {"extra": 2},
        "servers": {"memory": {"unit": "fixture.service", "service": {"b": 2}}},
    }))
    result = load_config(root, {"GENESIS_HOME": str(home)})
    assert result == {
        "stage": "writes", "adapter": {"argv": ["fixture"], "extra": 2},
        "servers": {"memory": {"url": "http://127.0.0.1:8101/mcp",
                               "unit": "fixture.service", "service": {"b": 2}}},
    }


@pytest.mark.parametrize("layer,error", [
    ("[]", None), ("{broken", None),
    ('{"servers":{"memory":false}}', None),
    ('{"servers":[1]}', AttributeError),
])
def test_loader_retains_existing_invalid_layer_behavior(tmp_path, layer, error):
    from genesis.mcp.shared_config import SharedConfigError, load_config

    (tmp_path / "config").mkdir()
    (tmp_path / "config/mcp_shared.json").write_text(layer)
    with pytest.raises(error or SharedConfigError):
        load_config(tmp_path, {"GENESIS_HOME": str(tmp_path / "missing")})


def test_absent_files_and_home_resolution(tmp_path, monkeypatch):
    from genesis.mcp.shared_config import SharedConfigError, load_config, overlay_path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert overlay_path({}) == tmp_path / ".genesis/config/mcp_shared.json"
    assert overlay_path({"HOME": str(tmp_path / "other")}) == tmp_path / "other/.genesis/config/mcp_shared.json"
    assert load_config(tmp_path, {"GENESIS_HOME": str(tmp_path / "missing")}) == {
        "stage": "off", "adapter": {"argv": []}, "servers": {},
    }
    with pytest.raises(SharedConfigError):
        overlay_path({"GENESIS_HOME": "relative"})


def test_script_import_with_isolated_no_site_uses_only_stdlib(tmp_path):
    code = """
import runpy, sys
scope = runpy.run_path(sys.argv[1], run_name='fixture_import')
assert scope['load_config'](sys.argv[2], {'GENESIS_HOME': sys.argv[2]})['stage'] == 'off'
assert 'genesis.mcp.shared_config' in sys.modules
assert not any(name.startswith(('fastmcp', 'mcp', 'genesis.memory', 'genesis.runtime')) for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(SCRIPT), str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_deploy_snapshot_keeps_reader_and_cli_after_files_change(tmp_path):
    root = tmp_path / "checkout"
    reader = root / "src/genesis/mcp/shared_config.py"
    reader.parent.mkdir(parents=True)
    lib = root / "scripts/lib/shared_mcp_units.sh"
    lib.parent.mkdir(parents=True)
    shutil.copy(READER, reader)
    shutil.copy(SCRIPT, lib.with_name("mcp_rollout.py"))
    shutil.copy(LIB, lib)
    result = subprocess.run(["bash", "-c", '. "$1"; printf "%s" "$_SHARED_MCP_ROLLOUT_PY"',
                             "fixture", str(lib)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    captured = result.stdout
    reader.write_text("raise RuntimeError('new reader must not run')\n")
    lib.with_name("mcp_rollout.py").write_text("raise RuntimeError('new CLI must not run')\n")
    (root / "config").mkdir()
    (root / "config/mcp_shared.json").write_text(json.dumps({
        "servers": {"memory": {"unit": "genesis-mcp-fixture.service"}},
    }))
    result = subprocess.run([sys.executable, "-I", "-S", "-c", captured, "units", "--root", str(root)],
        env={**os.environ, "GENESIS_HOME": str(tmp_path / "private-home")},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["genesis-mcp-fixture.service"]
