"""scripts/install_browser_stack.sh: one run at a time, a resolvable HOME, and the
provisioner's own exit code. The provisioner is a stub interpreter in a fake repo."""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_browser_stack.sh"


@pytest.fixture
def repo(tmp_path):
    """A repo dir whose .venv/bin/python records its arguments and exits with $STUB_RC."""
    r = tmp_path / "repo"
    (r / ".venv" / "bin").mkdir(parents=True)
    py = r / ".venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho "$@" > "$STUB_LOG"\nexit "${STUB_RC:-0}"\n')
    py.chmod(0o755)
    return r


def _run(repo, tmp_path, *, rc=0, env_extra=None, drop_home=False):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "GENESIS_HOME": str(tmp_path / "gh"),
        "GENESIS_REPO_DIR": str(repo),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "STUB_RC": str(rc),
        **(env_extra or {}),
    }
    if drop_home:
        env.pop("HOME")
    return subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.parametrize("rc", [0, 1])
def test_runs_the_provisioner_and_passes_its_exit_code(repo, tmp_path, rc):
    r = _run(repo, tmp_path, rc=rc)
    assert r.returncode == rc, r.stdout + r.stderr
    assert (tmp_path / "stub.log").read_text().split() == ["-m", "genesis.browser.provision", "run"]


def test_a_second_run_exits_0_while_the_first_holds_the_lock(repo, tmp_path):
    lock_dir = tmp_path / "gh" / "locks"
    lock_dir.mkdir(parents=True)
    with open(lock_dir / "browser-stack.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _run(repo, tmp_path, rc=1)
    assert r.returncode == 0
    assert "another run is in progress" in r.stdout
    assert not (tmp_path / "stub.log").exists()


def test_no_interpreter_is_exit_75(repo, tmp_path):
    (repo / ".venv" / "bin" / "python").unlink()
    r = _run(repo, tmp_path)
    assert r.returncode == 75 and "no venv interpreter" in r.stdout


def test_an_unset_home_is_resolved_from_passwd(repo, tmp_path):
    r = _run(repo, tmp_path, drop_home=True, env_extra={"GENESIS_HOME": str(tmp_path / "gh")})
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_unit_template_runs_this_script_without_escalation():
    unit = (_SCRIPT.parent / "systemd" / "genesis-browser-stack.service.template").read_text()
    assert "ExecStart=/bin/bash __REPO_DIR__/scripts/install_browser_stack.sh" in unit
    assert "NoNewPrivileges=true" in unit and "Type=oneshot" in unit
    lines = [ln.strip() for ln in unit.splitlines() if not ln.strip().startswith("#")]
    assert "[Install]" not in lines  # started by its timer or an operator, never at login
    # The module sets TMPDIR for its download children only (CLAUDE.md: never in a unit).
    assert not any(ln.startswith("Environment") and "TMPDIR" in ln for ln in lines)


def test_venv_path_names_the_interpreter(repo, tmp_path):
    """install.sh accepts a VENV_PATH outside the checkout, as genesis.env does."""
    venv = tmp_path / "elsewhere"
    (venv / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").rename(venv / "bin" / "python")
    r = _run(repo, tmp_path, env_extra={"VENV_PATH": str(venv)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "stub.log").read_text().split() == ["-m", "genesis.browser.provision", "run"]


def test_the_timer_retries_daily_and_catches_up():
    timer = (_SCRIPT.parent / "systemd" / "genesis-browser-stack.timer.template").read_text()
    assert "OnCalendar=*-*-* " in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer  # the installers' timer loop enables it
