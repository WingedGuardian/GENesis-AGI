"""Behavioral tests for container_swap_activate_live (scripts/lib/container_swap.sh).

The function is sourced from the REAL lib and driven with a stubbed ``sudo`` on
PATH plus a ``CONTSWAP_CGROUP_BASE`` override, so the logic under test is the
shipped code: live-activate a running container's swap cgroup (write ``max`` when
it is ``0``), idempotent when already set, and a graceful no-op when the cgroup
knob is absent. This is the fix for incus applying ``limits.memory.swap`` only at
container start — a retrofit on a running container would otherwise silently
no-op until a restart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "container_swap.sh"
HOST_SETUP = REPO_ROOT / "scripts" / "host-setup.sh"

_SUDO_PASSTHROUGH = """#!/bin/bash
exec "$@"
"""

_SUDO_FAIL = """#!/bin/bash
# Simulate a host where the privileged cgroup write is refused.
exit 1
"""


def _run(tmp_path, *, name, sudo_stub=_SUDO_PASSTHROUGH):
    """Source the real lib and invoke the function with a stubbed sudo + cgroup base."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    sudo = bindir / "sudo"
    sudo.write_text(sudo_stub)
    sudo.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CONTSWAP_CGROUP_BASE": str(tmp_path / "cgroup"),
    }
    script = f'source "{LIB}"; container_swap_activate_live "{name}"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _swap_file(tmp_path, name, value):
    p = tmp_path / "cgroup" / f"lxc.payload.{name}" / "memory.swap.max"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value)
    return p


def test_activates_when_zero(tmp_path):
    """The incident state: memory.swap.max=0 on a running container -> write max."""
    swap = _swap_file(tmp_path, "genesis", "0")
    res = _run(tmp_path, name="genesis")
    assert res.returncode == 0, res.stderr
    assert swap.read_text().strip() == "max"
    assert "activated swap live" in res.stdout


def test_idempotent_when_already_max(tmp_path):
    """Already permitting swap -> no write, no activation message."""
    swap = _swap_file(tmp_path, "genesis", "max")
    res = _run(tmp_path, name="genesis")
    assert res.returncode == 0
    assert swap.read_text().strip() == "max"
    assert "activated swap live" not in res.stdout


def test_noop_when_cgroup_absent(tmp_path):
    """Container stopped / non-standard layout -> graceful no-op (config applies on start)."""
    res = _run(tmp_path, name="genesis")  # no swap file created
    assert res.returncode == 0
    assert "activated swap live" not in res.stdout
    assert "WARNING" not in res.stdout


def test_empty_name_is_noop(tmp_path):
    res = _run(tmp_path, name="")
    assert res.returncode == 0
    assert res.stdout.strip() == ""


def test_write_failure_warns_and_leaves_value(tmp_path):
    """A refused privileged write must warn loudly, not claim success."""
    swap = _swap_file(tmp_path, "genesis", "0")
    res = _run(tmp_path, name="genesis", sudo_stub=_SUDO_FAIL)
    assert res.returncode == 0
    assert swap.read_text().strip() == "0"  # unchanged
    assert "WARNING" in res.stdout
    assert "activated swap live" not in res.stdout


def test_lib_parses_clean():
    res = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_host_setup_parses_clean():
    """The host-setup.sh edit that sources + calls the lib must stay bash -n clean."""
    res = subprocess.run(["bash", "-n", str(HOST_SETUP)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
