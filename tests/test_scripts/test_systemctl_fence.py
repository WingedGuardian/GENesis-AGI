"""The autouse fence in conftest.py keeps script tests away from the real
`systemctl --user` (issue #2863)."""

import os
import shutil
import subprocess

import pytest

# Captured at import (collection), before any per-test fixture runs.
_BUS_AT_IMPORT = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}


@pytest.mark.user_manager
def test_a_user_manager_test_keeps_the_bus_but_systemctl_stays_shimmed():
    for k, v in _BUS_AT_IMPORT.items():
        assert os.environ.get(k) == v, k
    found = shutil.which("systemctl")
    assert found is not None and "systemctl-fence" in found, found


def test_systemctl_resolves_to_the_fence_shim():
    found = shutil.which("systemctl")
    assert found is not None and "systemctl-fence" in found, found


def test_the_session_bus_points_nowhere():
    xdg = os.environ["XDG_RUNTIME_DIR"]
    assert "systemctl-fence" in xdg and not os.path.exists(xdg)
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={xdg}/bus"


def test_a_script_seeding_its_own_bus_default_still_cannot_connect():
    """deploy_code_only.sh / update.sh style: `${XDG_RUNTIME_DIR:-/run/user/$uid}`
    keeps the fence's value, and the real systemctl (called by absolute path, so
    the PATH shim is bypassed on purpose) cannot reach the user manager."""
    real = "/usr/bin/systemctl"
    if not os.path.exists(real):
        return
    script = (
        'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" '
        + real
        + " --user is-active genesis-systemctl-fence-probe.service"
    )
    proc = subprocess.run(
        ["bash", "-c", script], env=dict(os.environ), capture_output=True, text=True
    )
    assert "Failed to connect to bus" in proc.stderr, proc


def test_a_script_calling_systemctl_reaches_the_shim_and_is_refused():
    # A harmless probe on purpose: if the fence were missing, this would reach the
    # real user manager, so it must never be a command that changes anything there
    # (an unfenced `stop genesis-server` here once stopped a live server).
    probe = "--user is-active genesis-systemctl-fence-probe.service"
    proc = subprocess.run(
        ["bash", "-c", f"systemctl {probe}"],
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 3, proc
    assert "fenced" in proc.stderr, proc.stderr
    log = os.path.join(os.path.dirname(shutil.which("systemctl")), "calls.log")
    with open(log) as f:
        assert probe in f.read().splitlines()
