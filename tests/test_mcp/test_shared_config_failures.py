"""Real isolated CLI/capture failure contracts; no runtime or operator actions."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def owned_sources(tmp_path):
    root = tmp_path / "checkout with spaces"
    reader = root / "src/genesis/mcp/shared_config.py"
    reader.parent.mkdir(parents=True)
    cli = root / "scripts/lib/mcp_rollout.py"
    cli.parent.mkdir(parents=True)
    for target in (reader, cli, cli.with_name("shared_mcp_units.sh")):
        shutil.copy(ROOT / target.relative_to(root), target)
    return root, reader, cli


def break_reader(reader, state):
    if state == "missing":
        reader.unlink()
    elif state == "invalid":
        reader.write_text("def invalid syntax\n")
    else:
        reader.parents[1].joinpath("__init__.py").write_text("raise RuntimeError('synthetic package failure')\n")


@pytest.mark.parametrize("state", ["missing", "invalid", "package-error"])
def test_failed_reader_import_preserves_launch_argv(tmp_path, state):
    root, reader, cli = owned_sources(tmp_path)
    break_reader(reader, state)
    result = subprocess.run([
        sys.executable, "-I", "-S", str(cli), "launch", "--root", str(root), "--",
        sys.executable, "-I", "-S", "-c", "import json,sys;print(json.dumps(sys.argv[1:]))",
        "fixture argument", "--literal", "--", "tail",
    ], env={**os.environ, "GENESIS_HOME": str(tmp_path / "home")},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["fixture argument", "--literal", "--", "tail"]
    assert "launching the per-session server" in result.stderr


@pytest.mark.parametrize("command", ["units", "status"])
def test_failed_reader_import_cannot_report_nonlaunch_success(tmp_path, command):
    root, reader, cli = owned_sources(tmp_path)
    break_reader(reader, "missing")
    result = subprocess.run([sys.executable, "-I", "-S", str(cli), command, "--root", str(root)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not result.stdout


def test_shared_reader_uses_utf8_in_a_real_ascii_process(tmp_path):
    root, _, cli = owned_sources(tmp_path)
    (root / "config").mkdir()
    (root / "config/mcp_shared.json").write_text(
        '{"adapter":{"argv":["fixture-\\u00e9"]}}'.replace("\\u00e9", "\u00e9"), encoding="utf-8")
    code = """
import locale,runpy,sys
assert sys.flags.utf8_mode == 0
assert locale.getencoding() == 'ANSI_X3.4-1968'
scope=runpy.run_path(sys.argv[1],run_name='fixture_import')
config=scope['load_config'](sys.argv[2],{'GENESIS_HOME':sys.argv[3]})
assert config['adapter']['argv'] == ['fixture-\\u00e9']
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8=0", "-c", code,
        str(cli), str(root), str(tmp_path / "missing")],
        env={**os.environ, "LC_ALL": "C"}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("component", ["reader", "cli"])
def test_missing_startup_snapshot_can_use_complete_post_sync_sources(tmp_path, component):
    root, reader, cli = owned_sources(tmp_path)
    missing = reader if component == "reader" else cli
    missing.rename(missing.with_name(missing.name + ".arrives"))
    (root / "config").mkdir()
    (root / "config/mcp_shared.json").write_text(json.dumps({
        "servers": {"memory": {"unit": "genesis-mcp-fixture.service"}},
    }))
    shims = tmp_path / "bin"
    shims.mkdir()
    calls = tmp_path / "calls"
    shim = shims / "systemctl"
    shim.write_text('#!/bin/sh\nprintf "called\\n" >> "$FIXTURE_CALLS"\nprintf "LoadState=not-found\\n"\n')
    shim.chmod(0o755)
    result = subprocess.run(["bash", "-c", """
. "$1/scripts/lib/shared_mcp_units.sh"
test -z "$_SHARED_MCP_ROLLOUT_PY" || exit 97
mv "$2.arrives" "$2"
genesis_restart_shared_mcp_units "$1"
""", "fixture", str(root), str(missing)], env={**os.environ,
        "PATH": str(shims) + os.pathsep + os.environ["PATH"], "FIXTURE_CALLS": str(calls),
        "GENESIS_HOME": str(tmp_path / "home")}, capture_output=True, text=True, timeout=10)
    assert missing.is_file(), result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == ["called"]


@pytest.mark.parametrize("approved_hooks", [False, True])
def test_candidate_gate_refuses_its_shared_reader_dependency(approved_hooks):
    # Import only the pure path classifier; never execute deploy_candidates.
    code = """
import sys
sys.path.insert(0,sys.argv[1])
from deploy_candidates_gate import path_refusal
reason=path_refusal('src/genesis/mcp/shared_config.py',sys.argv[2]=='yes')
assert reason and 'src/genesis/mcp/shared_config.py' in reason
assert path_refusal('src/genesis/mcp/ordinary_fixture.py') is None
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT / "scripts"),
        "yes" if approved_hooks else "no"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
