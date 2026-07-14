"""Tests for scripts/code_intel_runner.sh — the idle-gated marker consumer.

The runner is the ONLY thing that turns a queued index request into an actual
index, and only when the box is idle. These tests drive it with a FAKE
entrypoint (CODE_INTEL_ENTRYPOINT seam) that returns a chosen rc, and fake
pressure readings (CODE_INTEL_FAKE_* seams), so no real indexing, load, or
systemd is needed. They lock down the rc contract that keeps the host freeze
safe and the index from being euthanized:

  * idle gate defers when the box is busy (marker kept)
  * rc 0  -> consume; rc 75 (frozen) / rc 3 (tool missing) -> keep, no penalty
  * genuine failure -> attempts++ -> .failed at the cap
  * escalated-full failure -> fall back to fast + back off, no penalty
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "scripts" / "code_intel_runner.sh"
_MARKER_PY = _REPO_ROOT / "scripts" / "lib" / "index_marker.py"

_spec = importlib.util.spec_from_file_location("index_marker", _MARKER_PY)
im = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(im)

_REPO = "/home/ubuntu/genesis"  # canonical; hash is stable


def _fake_entrypoint(tmp_path: Path, rc: int) -> Path:
    """A stand-in entrypoint: log argv (esp. the mode arg) and exit `rc`."""
    p = tmp_path / "fake_entry.sh"
    p.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "ENTRY repo=$1 tools=$2 mode=$3" >> "{tmp_path}/entry.log"\n'
        f"exit {rc}\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _run_runner(
    tmp_path: Path, entry_rc: int, *, load="0.1", iowait="0", claude_cpu="0", extra_env=None
):
    home = tmp_path / ".genesis"
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(home),
        "CODE_INTEL_ENTRYPOINT": str(_fake_entrypoint(tmp_path, entry_rc)),
        "CODE_INTEL_FAKE_LOADAVG": load,
        "CODE_INTEL_FAKE_IOWAIT": iowait,
        "CODE_INTEL_FAKE_CLAUDE_CPU": claude_cpu,
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(_RUNNER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _seed_marker(tmp_path, tools="both", mode="fast"):
    """Write a marker into the runner's GENESIS_HOME and return its hash."""
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    subprocess.run(
        ["python3", str(_MARKER_PY), "write", "--repo", _REPO, "--tools", tools, "--mode", mode],
        env=env,
        check=True,
        capture_output=True,
    )
    return im.marker_hash(_REPO)


def _markers(tmp_path):
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    out = subprocess.run(
        ["python3", str(_MARKER_PY), "list"],
        env=env,
        capture_output=True,
        text=True,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def _mdir(tmp_path) -> Path:
    return tmp_path / ".genesis" / "index-requests"


# ── idle gate ──────────────────────────────────────────────────────────────


def test_busy_box_defers_no_index(tmp_path):
    _seed_marker(tmp_path)
    res = _run_runner(tmp_path, entry_rc=0, load="9.0")
    assert res.returncode == 0
    assert not (tmp_path / "entry.log").exists()  # entrypoint never invoked
    assert len(_markers(tmp_path)) == 1  # marker kept


def test_high_iowait_defers(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=0, iowait="80")
    assert not (tmp_path / "entry.log").exists()
    assert len(_markers(tmp_path)) == 1


def test_busy_cc_session_defers(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=0, claude_cpu="95")
    assert not (tmp_path / "entry.log").exists()
    assert len(_markers(tmp_path)) == 1


def test_starved_marker_runs_under_relaxed_gate(tmp_path):
    h = _seed_marker(tmp_path)
    # Backdate requested_at so age > relax window; a moderate load then passes.
    import json

    mp = _mdir(tmp_path) / f"{h}.json"
    data = json.loads(mp.read_text())
    data["requested_at"] = 0  # ancient
    mp.write_text(json.dumps(data))
    _run_runner(
        tmp_path,
        entry_rc=0,
        load="3.0",  # >2 strict, <6 relaxed
        extra_env={"CODE_INTEL_RUNNER_RELAX_AFTER_S": "1"},
    )
    assert (tmp_path / "entry.log").exists()  # relaxed gate let it run
    assert _markers(tmp_path) == []  # consumed


# ── rc contract ────────────────────────────────────────────────────────────


def test_rc0_consumes_marker(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=0)
    assert _markers(tmp_path) == []


def test_rc75_frozen_keeps_marker(tmp_path):
    _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=75)
    assert len(_markers(tmp_path)) == 1  # host-frozen: marker survives


def test_rc3_tool_missing_keeps_marker_no_penalty(tmp_path):
    h = _seed_marker(tmp_path)
    _run_runner(tmp_path, entry_rc=3)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    assert listed[0].split("\t")[4] == "0"  # attempts NOT incremented
    assert not (_mdir(tmp_path) / f"{h}.failed.json").exists()


def test_genuine_failure_increments_attempts(tmp_path):
    h = _seed_marker(tmp_path, mode="fast")
    # Suppress escalation so rc1 is treated as a genuine fast-run failure.
    subprocess.run(
        ["python3", str(_MARKER_PY), "stamp-full", "--hash", h],
        env={**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")},
        check=True,
        capture_output=True,
    )
    _run_runner(tmp_path, entry_rc=1)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    assert listed[0].split("\t")[4] == "1"  # attempts incremented


def test_repeated_failures_euthanize(tmp_path):
    h = _seed_marker(tmp_path, mode="fast")
    env_home = {**os.environ, "GENESIS_HOME": str(tmp_path / ".genesis")}
    subprocess.run(
        ["python3", str(_MARKER_PY), "stamp-full", "--hash", h],
        env=env_home,
        check=True,
        capture_output=True,
    )
    for _ in range(im.MAX_ATTEMPTS):
        _run_runner(tmp_path, entry_rc=1)
    assert _markers(tmp_path) == []
    assert (_mdir(tmp_path) / f"{h}.failed.json").exists()


# ── escalation ─────────────────────────────────────────────────────────────


def test_escalates_fast_marker_to_full_when_due(tmp_path):
    _seed_marker(tmp_path, mode="fast")  # no .last-full → due
    _run_runner(tmp_path, entry_rc=0)
    assert "mode=full" in (tmp_path / "entry.log").read_text()


def test_escalated_full_failure_falls_back_no_penalty(tmp_path):
    h = _seed_marker(tmp_path, mode="fast")  # escalates to full, then fails
    _run_runner(tmp_path, entry_rc=1)
    listed = _markers(tmp_path)
    assert len(listed) == 1
    # marker stays fast, attempts NOT burned, and full is backed off
    assert listed[0].split("\t")[3] == "fast"
    assert listed[0].split("\t")[4] == "0"
    assert (_mdir(tmp_path) / f".full-backoff-{h}").exists()


def test_no_markers_is_quiet_noop(tmp_path):
    res = _run_runner(tmp_path, entry_rc=0)
    assert res.returncode == 0
    assert not (tmp_path / "entry.log").exists()
