"""Fixtures shared by more than one test module in this directory."""

import os

import pytest

from tests.test_scripts._deploy_candidates_world import (  # noqa: F401  (deploy_candidates)
    dc,
    dc_ready,
    dc_world,
)
from tests.test_scripts._deploy_station import station  # noqa: F401  (the deploy script's fixture)

# The variables through which `systemctl --user` reaches the live user manager. The
# fence points both at a path that does not exist, so `systemctl --user` cannot
# connect ("Failed to connect to bus"), whether a test copies os.environ or a script
# under test fills in its own default for an unset variable.


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "user_manager: the test needs a reachable systemd user manager for a read-only "
        "or self-contained operation (systemd-analyze verify, a transient scope); the "
        "fence keeps the session-bus variables but still shims systemctl",
    )


@pytest.fixture(scope="session")
def _systemctl_fence_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("systemctl-fence")
    shim = d / "systemctl"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{d / "calls.log"}"\n'
        'echo "systemctl is fenced in tests/test_scripts: $*" >&2\n'
        "exit 3\n"
    )
    shim.chmod(0o755)
    return d


@pytest.fixture(autouse=True)
def _fence_systemctl(_systemctl_fence_dir, monkeypatch, request):
    """Keep every script test away from the real `systemctl --user` (issue #2863).

    Scripts under test call it: restore.sh stops genesis-server before a SQLite
    restore, backup.sh stops services after a DB quarantine. A test that inherited
    the session's bus once stopped a live install's server on each of 10 runs. Two
    layers, because a test may build its env either way: the session-bus variables
    point at a path that does not exist, and a refusing, logging shim goes first on PATH (exit 3, the code
    `is-active` gives for a unit that is not running). A test's own stub still wins
    when the test puts its directory ahead of PATH. A test marked `user_manager`
    keeps the bus variables, for read-only or self-contained work such as
    `systemd-analyze --user verify` or a transient `systemd-run --user --scope`; the
    systemctl shim still goes first on PATH for it. A test that truly needs the real
    systemctl must restore PATH itself.
    """
    monkeypatch.setenv("PATH", f"{_systemctl_fence_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    if request.node.get_closest_marker("user_manager"):
        return
    # Point the bus at a path that does not exist rather than unsetting it: scripts
    # such as deploy_code_only.sh and update.sh seed `${XDG_RUNTIME_DIR:-/run/user/$uid}`
    # before calling systemctl, so an UNSET variable is recreated as the live route,
    # while a set one is kept and leads nowhere.
    nobus = _systemctl_fence_dir / "no-bus"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(nobus))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={nobus}/bus")
