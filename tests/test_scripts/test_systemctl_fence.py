"""The suite-wide live fence keeps script tests away from the real
`systemctl --user` (issue #2863; now `tests/_live_fence.py`)."""

import os
import shutil
import subprocess

import pytest

from tests import _live_fence as fence


def _require_installed_systemctl():
    """The fence stubs a tool only where it is installed, so without systemctl
    there is no stub to find and a bare call fails as on any such host."""
    if "systemctl" not in fence._S.stubbed:
        pytest.skip("systemctl is not installed here, so the fence installs no stub")


@pytest.mark.user_manager
def test_a_user_manager_test_keeps_the_bus_but_systemctl_stays_stubbed():
    for k, v in fence._S.saved_bus.items():
        assert os.environ.get(k) == v, k
    _require_installed_systemctl()
    found = shutil.which("systemctl")
    assert found is not None and found.startswith(fence._S.base), found


def test_systemctl_resolves_to_the_fence_stub():
    _require_installed_systemctl()
    found = shutil.which("systemctl")
    assert found is not None and found.startswith(fence._S.base), found


def test_the_session_bus_points_nowhere():
    xdg = os.environ["XDG_RUNTIME_DIR"]
    assert xdg.startswith(fence._S.base) and not os.path.exists(xdg)
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={xdg}/bus"


@pytest.mark.real_tool("systemctl")
def test_a_script_seeding_its_own_bus_default_still_cannot_connect():
    """deploy_code_only.sh / update.sh style: `${XDG_RUNTIME_DIR:-/run/user/$uid}`
    keeps the fence's value, and the real systemctl (called by absolute path, so
    the stub is bypassed on purpose) cannot reach the user manager."""
    real = "/usr/bin/systemctl"
    if not os.path.exists(real):
        pytest.skip("no systemctl here")
    script = (
        'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" '
        + real
        + " --user is-active genesis-systemctl-fence-probe.service"
    )
    proc = subprocess.run(
        ["bash", "-c", script], env=dict(os.environ), capture_output=True, text=True
    )
    assert "Failed to connect to bus" in proc.stderr, proc


def test_a_script_calling_systemctl_reaches_the_stub_and_is_answered():
    _require_installed_systemctl()
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
    assert proc.returncode == 1, proc  # what the real tool exits with no bus
    assert "Failed to connect to bus" in proc.stderr, proc.stderr
    lines = fence._drain()
    assert any(f"\tanswered\tsystemctl {probe}" in ln for ln in lines), lines
    fence._pending.extend(lines)
