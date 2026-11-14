"""Exercise the config fixture in real, isolated serial and xdist pytest runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROBE = '''
import json
from pathlib import Path
from genesis.cc.session_config import SessionConfigBuilder

def test_first(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_FIXTURE_PROBE", "1")
    monkeypatch.undo()
    generated = Path(SessionConfigBuilder().build_mcp_config("research"))
    assert generated.is_relative_to(tmp_path), "generated output escaped test temp"
    assert set(json.loads(generated.read_text())["mcpServers"]) == {
        "genesis-health", "genesis-memory", "genesis-recon",
    }
    (tmp_path / "first.json").write_text(str(generated))

def test_second(tmp_path):
    directory = tmp_path / "generated-mcp"
    assert not directory.exists(), "fixture reused another test's output"
    generated = Path(SessionConfigBuilder().build_mcp_config("research"))
    assert generated.parent == directory
'''


@pytest.mark.parametrize("workers", [0, 2])
def test_fixture_survives_undo_and_keeps_test_outputs_separate(tmp_path, workers):
    """Only this plugin is loaded, so undo cannot damage unrelated fixtures."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(_REPO), str(_REPO / "src"))),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    args = [sys.executable, "-m", "pytest", "-q", "-p", "tests._session_config"]
    if workers:
        args += ["-p", "xdist.plugin", "-n", str(workers)]
    result = subprocess.run(
        [*args, "--basetemp", str(tmp_path / "pt"), str(probe)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    generated = list((tmp_path / "pt").rglob("research_mcp.json"))
    assert len(generated) == 2
    assert len({path.parent for path in generated}) == 2
    for path in generated:
        assert set(json.loads(path.read_text())["mcpServers"]) == {
            "genesis-health", "genesis-memory", "genesis-recon",
        }
