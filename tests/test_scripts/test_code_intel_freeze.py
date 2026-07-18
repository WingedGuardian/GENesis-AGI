"""Tests for scripts/code_intel_freeze.sh — the managed indexing kill-switch.

The freeze holds BOTH single-flight locks that gate code-intel indexing:
  1. the main repo's per-repo lock (same path code_intel_index.sh derives), so a
     manual/hook entrypoint run for that repo exits CODE_INTEL_INDEX_LOCK_SKIP_RC
     (the runner's 75 → "host-frozen, keep the marker") instead of indexing;
  2. the runner's self-lock (code_intel_runner.sh), so an idle tick can't process
     ANY marker while armed.

The headline test arms the REAL freeze and runs the REAL entrypoint against it,
so any drift between the two scripts' lock-path recipes fails CI here rather than
silently turning the kill-switch into a no-op. Everything is binary-independent
(fake indexer via PATH injection) so it runs in a toolless CI runner.
"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FREEZE = _REPO_ROOT / "scripts" / "code_intel_freeze.sh"
_ENTRYPOINT = _REPO_ROOT / "scripts" / "lib" / "code_intel_index.sh"
_TEMPLATE = _REPO_ROOT / "scripts" / "systemd" / "genesis-code-intel-freeze.service.template"

_SYSTEM_PATH = "/usr/bin:/bin"


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_repo(tmp_path: Path) -> Path:
    repo = Path(os.path.realpath(tmp_path / "repo"))
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    return repo


def _lock_dir(tmp_path: Path) -> Path:
    return tmp_path / ".genesis" / "locks"


def _repo_lock_path(tmp_path: Path, repo: Path) -> Path:
    """Recompute the repo lock path the SAME way the shell does (shell out to
    sha1sum so this test can't drift from the scripts by reimplementing it)."""
    digest = subprocess.run(
        ["bash", "-c", 'printf "%s" "$1" | sha1sum | cut -c1-16', "_", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return _lock_dir(tmp_path) / f"code-intel-{digest}.lock"


def _arm_freeze(tmp_path: Path, repo: Path, logfile: Path) -> subprocess.Popen:
    """Start the freeze in the background; return once it has logged ARMED."""
    env = {
        "PATH": _SYSTEM_PATH,
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
    }
    fh = logfile.open("w")
    proc = subprocess.Popen(
        ["bash", str(_FREEZE), str(repo)],
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if "ARMED" in logfile.read_text():
            return proc
        if proc.poll() is not None:
            raise AssertionError(f"freeze exited early: {logfile.read_text()}")
        time.sleep(0.1)
    proc.kill()
    raise AssertionError(f"freeze never armed within 20s: {logfile.read_text()}")


def _disarm(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()  # SIGTERM → trap → releases locks
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_armed_freeze_makes_entrypoint_exit_skip_rc(tmp_path):
    """Armed freeze holds the repo lock → the real entrypoint exits 75."""
    repo = _make_repo(tmp_path)
    logfile = tmp_path / "freeze.log"
    proc = _arm_freeze(tmp_path, repo, logfile)
    try:
        env = {
            "PATH": _SYSTEM_PATH,
            "HOME": str(tmp_path),
            "GENESIS_HOME": str(tmp_path / ".genesis"),
            "CODE_INTEL_INDEX_LOCK_SKIP_RC": "75",
        }
        res = subprocess.run(
            ["bash", str(_ENTRYPOINT), str(repo), "cbm", "fast"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 75, (
            f"expected 75 (frozen), got {res.returncode}: {res.stdout}{res.stderr}"
        )
        assert "lock held" in res.stdout, res.stdout
    finally:
        _disarm(proc)


def test_armed_freeze_holds_runner_self_lock(tmp_path):
    """Armed freeze also holds the runner self-lock → an idle tick can't run.

    Verified directly: a non-blocking flock on the runner lock must fail while
    armed (the freeze holds it), and succeed once disarmed.
    """
    repo = _make_repo(tmp_path)
    logfile = tmp_path / "freeze.log"
    runner_lock = _lock_dir(tmp_path) / "code-intel-runner.lock"
    proc = _arm_freeze(tmp_path, repo, logfile)
    try:
        assert runner_lock.exists(), "freeze did not create the runner lock"
        with runner_lock.open("w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError("runner lock was NOT held while armed")
            except BlockingIOError:
                pass  # expected: the freeze holds it
    finally:
        _disarm(proc)
    # After disarm the runner lock is free again.
    with runner_lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(fh, fcntl.LOCK_UN)


def test_entrypoint_proceeds_after_disarm(tmp_path):
    """Once the freeze is stopped, the entrypoint indexes normally (rc=0)."""
    repo = _make_repo(tmp_path)
    logfile = tmp_path / "freeze.log"
    # Fake cbm so a real index "runs" without systemd or the real binary.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    toollog = tmp_path / "tool.log"
    _write_exec(
        fakebin / "codebase-memory-mcp", f'#!/usr/bin/env bash\necho "ran:$*" >> "{toollog}"\n'
    )
    # Minimal PATH (no systemd-run) → rlimit fallback path actually invokes the tool.
    minbin = tmp_path / "minbin"
    minbin.mkdir()
    for tool in (
        "bash",
        "sh",
        "env",
        "mkdir",
        "sha1sum",
        "cut",
        "flock",
        "nice",
        "date",
        "dirname",
    ):
        src = Path("/usr/bin") / tool
        if not src.exists():
            src = Path("/bin") / tool
        if src.exists() and not (minbin / tool).exists():
            (minbin / tool).symlink_to(src)
    path = f"{fakebin}:{minbin}"

    proc = _arm_freeze(tmp_path, repo, logfile)
    _disarm(proc)  # disarm immediately, then the entrypoint should proceed

    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        "CODE_INTEL_INDEX_LOCK_SKIP_RC": "75",
        "CODE_INTEL_WATCHDOG_INTERVAL": "1",
        "CODE_INTEL_WATCHDOG_WARMUP_S": "0",
        "CODE_INTEL_FAKE_LOADAVG": "0",
        "CODE_INTEL_FAKE_IOWAIT": "0",
    }
    res = subprocess.run(
        ["bash", str(_ENTRYPOINT), str(repo), "cbm", "fast"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"{res.returncode}: {res.stdout}{res.stderr}"
    assert "ran:" in toollog.read_text(), "fake cbm did not run after disarm"


# ── template sanity ────────────────────────────────────────────────────────


def test_template_kill_then_seal_and_install():
    text = _TEMPLATE.read_text(encoding="utf-8")
    # ExecStartPre stops in-flight scopes (kill-then-seal), ExecStart runs the script.
    assert "ExecStartPre=" in text and "stop code-intel-*.scope" in text
    assert "scripts/code_intel_freeze.sh" in text
    # Boot-persistence install section present (enable → arm on reboot).
    assert "WantedBy=default.target" in text
    # Only the known sed placeholders appear.
    import re

    placeholders = set(re.findall(r"__[A-Z_]+__", text))
    assert placeholders <= {"__HOME__", "__VENV__", "__REPO_DIR__", "__CC_BIN_DIR__"}, placeholders
