"""Tests for repo_routing_guard.py — wrong-repo git add/commit backstop.

Exit codes: 0 = allowed (silent or advisory), 2 = blocked. Advisory emits
hookSpecificOutput.additionalContext on stdout with exit 0.

The guard must NEVER block legitimate work — fail-open is exercised explicitly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_TOPOLOGY = textwrap.dedent("""\
    version: 1
    repos:
      GENesis-AGI:
        remotes: [WingedGuardian/GENesis-AGI]
        allow_paths:
          - dashboard/routes/voice_api.py
          - src/genesis/channels/voice/
          - src/genesis/attention/
      GENesis-Voice:
        remotes: [WingedGuardian/GENesis-Voice]
        strong_path_segments: [firmware, esphome, s2s_bridge, ambient_bridge, omi]
        strong_path_globs: ["config/voice-pe/**", "**/flash_*.sh"]
        strong_content_markers: ["esphome:", "ESP32", "esp-idf", "platformio"]
        weak_path_segments: [voice-pe, voice_pe, satellite, edge]
    settings:
      max_files: 200
      max_content_files: 20
      max_content_bytes: 4096
      git_timeout_seconds: 5
""")


def _guard_script() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        s = ancestor / "scripts" / "hooks" / "repo_routing_guard.py"
        if s.exists():
            return s
    raise FileNotFoundError("repo_routing_guard.py not found")


@pytest.fixture(scope="module")
def topology(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("topo") / "repo_topology.yaml"
    p.write_text(_TOPOLOGY)
    return str(p)


def _make_repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.io"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    return path


def _write(repo: Path, rel: str, content: str = "x\n") -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)


def _run(cmd: str, cwd: Path, topology: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_TOOL_INPUT": json.dumps({"command": cmd})}
    if topology is not None:
        env["REPO_TOPOLOGY_PATH"] = topology
    else:
        env.pop("REPO_TOPOLOGY_PATH", None)
    return subprocess.run(
        [sys.executable, str(_guard_script())],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=20,
    )


AGI = "https://github.com/WingedGuardian/GENesis-AGI.git"


@pytest.fixture
def agi_repo(tmp_path) -> Path:
    return _make_repo(tmp_path / "agi", AGI)


# ── STRONG → block ──────────────────────────────────────────────────────

def test_firmware_add_blocks(agi_repo, topology):
    _write(agi_repo, "firmware/boot.py")
    r = _run("git add firmware/boot.py", agi_repo, topology)
    assert r.returncode == 2
    assert "GENesis-Voice" in r.stderr and "firmware/boot.py" in r.stderr


def test_staged_firmware_commit_blocks(agi_repo, topology):
    _write(agi_repo, "esphome/device.yaml", "esphome:\n  name: x\n")
    subprocess.run(["git", "-C", str(agi_repo), "add", "esphome/device.yaml"], check=True)
    r = _run("git commit -m 'add device'", agi_repo, topology)
    assert r.returncode == 2
    assert "GENesis-Voice" in r.stderr


def test_add_dot_porcelain_blocks(agi_repo, topology):
    _write(agi_repo, "s2s_bridge/app.py")
    _write(agi_repo, "README.md")
    r = _run("git add .", agi_repo, topology)
    assert r.returncode == 2
    assert "s2s_bridge/app.py" in r.stderr


def test_glob_flash_script_blocks(agi_repo, topology):
    _write(agi_repo, "scripts/flash_device.sh", "#!/bin/sh\n")
    r = _run("git add scripts/flash_device.sh", agi_repo, topology)
    assert r.returncode == 2


def test_content_marker_new_file_blocks(agi_repo, topology):
    # Path has no marker segment, but content does (ESP32) — new file only.
    _write(agi_repo, "src/genesis/hardware/driver.py", "# ESP32 pin config\n")
    r = _run("git add src/genesis/hardware/driver.py", agi_repo, topology)
    assert r.returncode == 2


def test_worktree_dash_C_blocks(agi_repo, topology, tmp_path):
    # Run the guard from an unrelated cwd; target the repo via `git -C`.
    _write(agi_repo, "omi/connector.py")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = _run(f"git -C {agi_repo} add omi/connector.py", elsewhere, topology)
    assert r.returncode == 2


def test_cd_prefix_blocks(agi_repo, topology, tmp_path):
    _write(agi_repo, "ambient_bridge/x.py")
    elsewhere = tmp_path / "elsewhere2"
    elsewhere.mkdir()
    r = _run(f"cd {agi_repo} && git add ambient_bridge/x.py", elsewhere, topology)
    assert r.returncode == 2


# ── WEAK → advisory (exit 0, additionalContext) ─────────────────────────

def test_weak_marker_advises(agi_repo, topology):
    _write(agi_repo, "docs/satellite/notes.md")
    r = _run("git add docs/satellite/notes.md", agi_repo, topology)
    assert r.returncode == 0
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "GENesis-Voice" in ctx and "satellite" in ctx


# ── allow_paths silence WEAK, clean files silent ────────────────────────

def test_allow_path_voice_api_silent(agi_repo, topology):
    _write(agi_repo, "dashboard/routes/voice_api.py")
    r = _run("git add dashboard/routes/voice_api.py", agi_repo, topology)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_allow_path_channels_voice_silences_weak(agi_repo, topology):
    # 'edge' is a weak marker, but channels/voice/ is allow-listed.
    _write(agi_repo, "src/genesis/channels/voice/edge_client.py")
    r = _run("git add src/genesis/channels/voice/edge_client.py", agi_repo, topology)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_clean_agi_file_silent(agi_repo, topology):
    _write(agi_repo, "src/genesis/memory/retrieval.py")
    r = _run("git add src/genesis/memory/retrieval.py", agi_repo, topology)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_strong_still_fires_under_allow_path(agi_repo, topology):
    # channels/voice/ is allow-listed but s2s_bridge is a STRONG marker: the
    # wrong-repo component name still blocks (this IS the incident shape).
    _write(agi_repo, "src/genesis/channels/voice/s2s_bridge/app.py")
    r = _run("git add src/genesis/channels/voice/s2s_bridge/app.py", agi_repo, topology)
    assert r.returncode == 2


# ── override ────────────────────────────────────────────────────────────

def test_override_bypasses(agi_repo, topology):
    _write(agi_repo, "firmware/boot.py")
    r = _run("git add firmware/boot.py  # repo-routing-override", agi_repo, topology)
    assert r.returncode == 0
    assert "override acknowledged" in r.stderr


# ── fail-open cases (all exit 0) ────────────────────────────────────────

def test_non_git_command_passes(agi_repo, topology):
    assert _run("ls -la", agi_repo, topology).returncode == 0


def test_non_add_commit_git_passes(agi_repo, topology):
    _write(agi_repo, "firmware/boot.py")
    assert _run("git status", agi_repo, topology).returncode == 0


def test_empty_topology_fails_open(agi_repo, tmp_path):
    # A topology with no repos declared must not guard (fail-open).
    empty = tmp_path / "empty.yaml"
    empty.write_text("version: 1\nrepos: {}\n")
    _write(agi_repo, "firmware/boot.py")
    r = _run("git add firmware/boot.py", agi_repo, str(empty))
    assert r.returncode == 0


def test_missing_topology_falls_back_to_in_repo(agi_repo, tmp_path):
    # A bad REPO_TOPOLOGY_PATH falls back to the in-repo config, which still
    # guards — the guard is never silently disabled by a stale env var.
    _write(agi_repo, "firmware/boot.py")
    r = _run("git add firmware/boot.py", agi_repo, str(tmp_path / "nope.yaml"))
    assert r.returncode == 2


def test_malformed_tool_input_fails_open(agi_repo, topology):
    env = {**os.environ, "CLAUDE_TOOL_INPUT": "{not json",
           "REPO_TOPOLOGY_PATH": topology}
    r = subprocess.run([sys.executable, str(_guard_script())], env=env,
                       cwd=str(agi_repo), capture_output=True, text=True, timeout=20)
    assert r.returncode == 0


def test_unknown_repo_fails_open(tmp_path, topology):
    other = _make_repo(tmp_path / "other", "https://github.com/someone/unrelated.git")
    _write(other, "firmware/boot.py")
    r = _run("git add firmware/boot.py", other, topology)
    assert r.returncode == 0


def test_no_remote_fails_open(tmp_path, topology):
    repo = tmp_path / "noremote"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _write(repo, "firmware/boot.py")
    r = _run("git add firmware/boot.py", repo, topology)
    assert r.returncode == 0


def test_empty_tool_input_passes(agi_repo, topology):
    env = {**os.environ, "REPO_TOPOLOGY_PATH": topology}
    env.pop("CLAUDE_TOOL_INPUT", None)
    r = subprocess.run([sys.executable, str(_guard_script())], env=env,
                       cwd=str(agi_repo), capture_output=True, text=True, timeout=20)
    assert r.returncode == 0
