"""Behavioral tests for container_swap_activate_live (scripts/lib/container_swap.sh).

The function is sourced from the REAL lib and driven under the caller's shell
mode (``set -euo pipefail``, as host-setup.sh uses) with a stubbed ``sudo`` on
PATH plus a ``CONTSWAP_CGROUP_BASE`` override. A ``__DONE__`` sentinel printed
after the call proves the function returned instead of tripping errexit — the
failure modes here (unreadable / vanished cgroup knob, refused write) must never
abort the host-setup run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "container_swap.sh"
HOST_SETUP = REPO_ROOT / "scripts" / "host-setup.sh"

# Passthrough sudo: run the wrapped command as-is.
_SUDO_PASSTHROUGH = """#!/bin/bash
exec "$@"
"""

# Passthrough except the privileged WRITE (tee) is refused — read succeeds,
# write fails.
_SUDO_TEE_FAIL = """#!/bin/bash
if [ "$1" = "tee" ]; then exit 1; fi
exec "$@"
"""

# The READ itself is refused (permission denied on the root-owned cgroup knob).
_SUDO_CAT_FAIL = """#!/bin/bash
if [ "$1" = "cat" ]; then exit 1; fi
exec "$@"
"""


def _run(tmp_path, *, name, sudo_stub=_SUDO_PASSTHROUGH):
    """Source the real lib and invoke the function under `set -euo pipefail`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    sudo = bindir / "sudo"
    sudo.write_text(sudo_stub)
    sudo.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CONTSWAP_CGROUP_BASE": str(tmp_path / "cgroup"),
    }
    script = (
        f'set -euo pipefail; source "{LIB}"; container_swap_activate_live "{name}"; echo __DONE__'
    )
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
    assert "__DONE__" in res.stdout  # did not errexit-abort
    assert swap.read_text().strip() == "max"
    assert "activated swap live" in res.stdout


def test_idempotent_when_already_max(tmp_path):
    """Already permitting swap -> no write, no activation message."""
    swap = _swap_file(tmp_path, "genesis", "max")
    res = _run(tmp_path, name="genesis")
    assert res.returncode == 0
    assert "__DONE__" in res.stdout
    assert swap.read_text().strip() == "max"
    assert "activated swap live" not in res.stdout


def test_noop_when_cgroup_absent(tmp_path):
    """Container stopped / non-standard layout -> graceful no-op (config applies on start)."""
    res = _run(tmp_path, name="genesis")  # no swap file created
    assert res.returncode == 0
    assert "__DONE__" in res.stdout
    assert "activated swap live" not in res.stdout
    assert "WARNING" not in res.stdout


def test_empty_name_is_noop(tmp_path):
    res = _run(tmp_path, name="")
    assert res.returncode == 0
    assert "__DONE__" in res.stdout


def test_write_failure_warns_and_leaves_value(tmp_path):
    """A refused privileged WRITE must warn loudly, not claim success."""
    swap = _swap_file(tmp_path, "genesis", "0")
    res = _run(tmp_path, name="genesis", sudo_stub=_SUDO_TEE_FAIL)
    assert res.returncode == 0
    assert "__DONE__" in res.stdout
    assert swap.read_text().strip() == "0"  # unchanged
    assert "WARNING" in res.stdout
    assert "activated swap live" not in res.stdout


def test_read_failure_is_safe_noop_under_errexit(tmp_path):
    """A refused READ must fall through to a no-op, NOT abort the caller's
    `set -e` run — regression guard for a bare `cur=$(cat ...)` that would
    propagate cat's nonzero status and errexit the whole host-setup."""
    swap = _swap_file(tmp_path, "genesis", "0")
    res = _run(tmp_path, name="genesis", sudo_stub=_SUDO_CAT_FAIL)
    assert res.returncode == 0, res.stderr
    assert "__DONE__" in res.stdout  # proves no errexit abort
    assert swap.read_text().strip() == "0"  # untouched
    assert "activated swap live" not in res.stdout


def test_host_setup_wires_the_lib():
    """The fix is dead unless host-setup.sh actually sources AND calls it."""
    text = HOST_SETUP.read_text()
    assert 'lib/container_swap.sh"' in text
    assert "container_swap_activate_live" in text


def test_lib_parses_clean():
    res = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_host_setup_parses_clean():
    """The host-setup.sh edit that sources + calls the lib must stay bash -n clean."""
    res = subprocess.run(["bash", "-n", str(HOST_SETUP)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
