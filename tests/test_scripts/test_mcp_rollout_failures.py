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
    cli = root / "scripts/lib/mcp_rollout.py"
    cli.parent.mkdir(parents=True)
    for target in (cli, cli.with_name("shared_mcp_units.sh")):
        shutil.copy(ROOT / target.relative_to(root), target)
    return root, cli


@pytest.mark.parametrize("state", ["missing", "invalid", "package-error"])
def test_standalone_cli_preserves_launch_argv_without_a_genesis_import(tmp_path, state):
    root, cli = owned_sources(tmp_path)
    package = root / "src/genesis"
    if state != "missing":
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "def invalid syntax\n" if state == "invalid" else
            "raise RuntimeError('synthetic package failure')\n")
    result = subprocess.run([
        sys.executable, "-I", "-S", str(cli), "launch", "--root", str(root), "--",
        sys.executable, "-I", "-S", "-c", "import json,sys;print(json.dumps(sys.argv[1:]))",
        "fixture argument", "--literal", "--", "tail",
    ], env={**os.environ, "GENESIS_HOME": str(tmp_path / "home")},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["fixture argument", "--literal", "--", "tail"]


def test_rollout_reads_utf8_in_a_real_ascii_process(tmp_path):
    root, cli = owned_sources(tmp_path)
    (root / "config").mkdir()
    (root / "config/mcp_shared.json").write_text(
        '{"adapter":{"argv":["fixture-\\u00e9"]}}'.replace("\\u00e9", "\u00e9"), encoding="utf-8")
    code = """
import codecs,locale,runpy,sys
assert sys.flags.utf8_mode == 0
if codecs.lookup(locale.getencoding()).name != "ascii":
    raise SystemExit(77)
scope=runpy.run_path(sys.argv[1],run_name='fixture_import')
config=scope['load_config'](sys.argv[2],{'GENESIS_HOME':sys.argv[3]})
assert config['adapter']['argv'] == ['fixture-\\u00e9']
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8=0", "-c", code,
        str(cli), str(root), str(tmp_path / "missing")],
        env={**os.environ, "LC_ALL": "C"}, capture_output=True, text=True, timeout=10)
    if result.returncode == 77:
        pytest.skip("platform cannot supply an ASCII locale for this control")
    assert result.returncode == 0, result.stderr


def test_missing_startup_snapshot_can_use_complete_post_sync_sources(tmp_path):
    root, cli = owned_sources(tmp_path)
    missing = cli
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
def test_candidate_gate_refuses_its_rollout_cli_dependency(approved_hooks):
    # Import only the pure path classifier; never execute deploy_candidates.
    code = """
import sys
sys.path.insert(0,sys.argv[1])
from deploy_candidates_gate import path_refusal
reason=path_refusal('scripts/lib/mcp_rollout.py',sys.argv[2]=='yes')
assert reason and 'scripts/lib/mcp_rollout.py' in reason
assert path_refusal('src/genesis/mcp/ordinary_fixture.py') is None
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT / "scripts"),
        "yes" if approved_hooks else "no"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
