"""Fail-closed version gate for the GitNexus MCP launcher."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / ".claude" / "mcp" / "run-gitnexus"


def _fake_gitnexus(tmp_path: Path, version: str) -> tuple[Path, Path]:
    binary = tmp_path / "gitnexus"
    log = tmp_path / "args.log"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "${{1:-}}" = "--version" ]; then echo "{version}"; exit 0; fi\n'
        f'printf "%s\\n" "$*" > "{log}"\n'
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, log


def _run(tmp_path: Path, version: str, *, node_version: str = "v22.22.2"):
    binary, log = _fake_gitnexus(tmp_path, version)
    node = tmp_path / "node"
    node.write_text(f"#!/bin/sh\necho {node_version}\n")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "GITNEXUS_BIN": str(binary),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        [str(LAUNCHER), "mcp"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log


def test_launcher_executes_only_the_pinned_binary(tmp_path):
    result, log = _run(tmp_path, "1.6.12")
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "mcp\n"


def test_launcher_refuses_a_stale_binary(tmp_path):
    result, log = _run(tmp_path, "1.6.8")
    assert result.returncode != 0
    assert "refusing unpinned GitNexus 1.6.8; expected 1.6.12" in result.stderr
    assert not log.exists()


def test_launcher_refuses_an_unsupported_node_runtime(tmp_path):
    result, log = _run(tmp_path, "1.6.12", node_version="v23.11.1")
    assert result.returncode != 0
    assert "does not support Node v23.11.1" in result.stderr
    assert not log.exists()


def test_launcher_resolves_gitnexus_from_npm_prefix_outside_path(tmp_path):
    path_bin = tmp_path / "path-bin"
    prefix_bin = tmp_path / "npm-prefix" / "bin"
    path_bin.mkdir()
    prefix_bin.mkdir(parents=True)
    binary, log = _fake_gitnexus(prefix_bin, "1.6.12")
    node = path_bin / "node"
    node.write_text("#!/bin/sh\necho v22.22.2\n")
    node.chmod(0o755)
    npm = path_bin / "npm"
    npm.write_text(f'#!/bin/sh\necho "{tmp_path / "npm-prefix"}"\n')
    npm.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{path_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
    }
    env.pop("GITNEXUS_BIN", None)
    result = subprocess.run(
        [str(LAUNCHER), "status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert binary == prefix_bin / "gitnexus"
    assert log.read_text() == "status\n"
