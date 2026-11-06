"""Self-contained stdlib configuration and captured rollout CLI behavior."""

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
LIB = REPO / "scripts/lib/shared_mcp_units.sh"


def rollout_module():
    spec = importlib.util.spec_from_file_location("shared_config_rollout", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loader_preserves_per_key_not_recursive_overlay_merge(tmp_path):
    load_config = rollout_module().load_config

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


@pytest.mark.parametrize("layer", [
    "[]", "{broken", '{"servers":{"memory":false}}', '{"servers":[1]}',
])
def test_invalid_layers_report_config_errors(tmp_path, layer):
    rollout = rollout_module()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/mcp_shared.json").write_text(layer)
    with pytest.raises(rollout.RolloutError):
        rollout.load_config(tmp_path, {"GENESIS_HOME": str(tmp_path / "missing")})


def test_absent_files_and_home_resolution(tmp_path, monkeypatch):
    rollout = rollout_module()
    load_config, overlay_path = rollout.load_config, rollout.overlay_path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert overlay_path({}) == tmp_path / ".genesis/config/mcp_shared.json"
    assert overlay_path({"HOME": str(tmp_path / "other")}) == tmp_path / "other/.genesis/config/mcp_shared.json"
    assert load_config(tmp_path, {"GENESIS_HOME": str(tmp_path / "missing")}) == {
        "stage": "off", "adapter": {"argv": []}, "servers": {},
    }
    with pytest.raises(rollout.RolloutError):
        overlay_path({"GENESIS_HOME": "relative"})


def test_script_import_with_isolated_no_site_uses_only_stdlib(tmp_path):
    code = """
import runpy, sys
scope = runpy.run_path(sys.argv[1], run_name='fixture_import')
assert scope['load_config'](sys.argv[2], {'GENESIS_HOME': sys.argv[2]})['stage'] == 'off'
assert not any(name == 'genesis' or name.startswith(('genesis.', 'fastmcp', 'mcp')) for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(SCRIPT), str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("replacement", ["replace", "delete"])
def test_deploy_snapshot_keeps_cli_after_file_changes(tmp_path, replacement):
    root = tmp_path / "checkout"
    lib = root / "scripts/lib/shared_mcp_units.sh"
    lib.parent.mkdir(parents=True)
    cli = lib.with_name("mcp_rollout.py")
    shutil.copy(SCRIPT, cli)
    shutil.copy(LIB, lib)
    (root / "config").mkdir()
    (root / "config/mcp_shared.json").write_text(json.dumps({
        "servers": {"memory": {"unit": "genesis-mcp-fixture.service"}},
    }))
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "unit-calls"
    shim = binary / "systemctl"
    shim.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE_CALLS"\nprintf "LoadState=not-found\\n"\n')
    shim.chmod(0o755)
    result = subprocess.run(["bash", "-c", '''
. "$1"
if [ "$4" = delete ]; then unlink "$2"; else printf '%s\\n' "$5" > "$2"; fi
genesis_restart_shared_mcp_units "$3"
''', "fixture", str(lib), str(cli), str(root), replacement,
        "raise RuntimeError('new CLI must not run')"],
        env={**os.environ, "GENESIS_HOME": str(tmp_path / "private-home"),
             "PATH": str(binary) + os.pathsep + os.environ["PATH"], "FIXTURE_CALLS": str(calls)},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines()[-1].endswith("genesis-mcp-fixture.service")


@pytest.mark.parametrize("state", ["missing", "empty", "invalid", "blank", "comments", "truncated"])
def test_deploy_refuses_incomplete_source_capture(tmp_path, state):
    root = tmp_path / "checkout"
    lib = root / "scripts/lib/shared_mcp_units.sh"
    lib.parent.mkdir(parents=True)
    cli = lib.with_name("mcp_rollout.py")
    shutil.copy(SCRIPT, cli)
    shutil.copy(LIB, lib)
    damaged = cli
    if state == "missing":
        damaged.unlink()
    else:
        damaged.write_text({
            "empty": "", "invalid": "def broken syntax\n", "blank": " \n\t\n",
            "comments": "# incomplete captured source\n",
            "truncated": "def main():\n    pass\n",
        }[state])
    (root / "config").mkdir()
    (root / "config/mcp_shared.json").write_text(json.dumps({
        "servers": {"memory": {"unit": "genesis-mcp-fixture.service"}},
    }))
    shims = tmp_path / "bin"
    shims.mkdir()
    calls = tmp_path / "systemctl-called"
    shim = shims / "systemctl"
    shim.write_text(f"#!/bin/sh\nprintf 'called\\n' >> '{calls}'\nexit 98\n")
    shim.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_restart_shared_mcp_units "$2"',
         "fixture", str(lib), str(root)],
        env={**os.environ, "PATH": str(shims) + os.pathsep + os.environ["PATH"],
             "GENESIS_HOME": str(tmp_path / "private-home")},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not calls.exists(), "incomplete source capture reached systemctl"
