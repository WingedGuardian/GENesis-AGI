"""Fixtures shared by more than one test module in this directory."""

import os

import pytest

from tests.test_scripts._deploy_candidates_world import (  # noqa: F401  (deploy_candidates)
    dc,
    dc_ready,
    dc_world,
)
from tests.test_scripts._deploy_station import station  # noqa: F401  (the deploy script's fixture)

# The variables through which `systemctl --user` reaches the live user manager. With
# both unset it cannot connect at all ("Failed to connect to bus: No medium found",
# measured on systemd 255), so a test env that is copied from os.environ, or built
# from scratch, never carries a route to the running services.
_SESSION_BUS_VARS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


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
def _fence_systemctl(_systemctl_fence_dir, monkeypatch):
    """Keep every script test away from the real `systemctl --user` (issue #2863).

    Scripts under test call it: restore.sh stops genesis-server before a SQLite
    restore, backup.sh stops services after a DB quarantine. A test that inherited
    the session's bus once stopped a live install's server on each of 10 runs. Two
    layers, because a test may build its env either way: the session-bus variables
    are removed, and a refusing, logging shim goes first on PATH (exit 3, the code
    `is-active` gives for a unit that is not running). A test's own stub still wins
    when the test puts its directory ahead of PATH. A test that truly needs the real
    systemctl must restore PATH and the bus variables itself.
    """
    monkeypatch.setenv("PATH", f"{_systemctl_fence_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for var in _SESSION_BUS_VARS:
        monkeypatch.delenv(var, raising=False)
