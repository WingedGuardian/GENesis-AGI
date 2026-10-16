"""One sandbox for every test of the dashboard update routes.

``genesis.dashboard.routes.updates`` binds its paths at import (``Path.home()``
and ``genesis_home()``) and launches real processes (update.sh, the update
orchestrator, Claude sessions) through its own ``subprocess`` name. An earlier
test in this area let a failing case start real update orchestrators on the
machine running the suite. So every module that imports the routes goes through
``isolate_update_routes`` (the ``updates_sandbox`` fixture in tests/conftest.py,
or a direct call), and tests/test_dashboard/test_update_sandbox_coverage.py
fails any module that does not.

It sets HOME and GENESIS_HOME to a temporary home, points every path constant
of the module there (or at this checkout's own read-only scripts), replaces the
module's ``subprocess`` with a namespace whose ``Popen`` records the argv and
raises, stubs the ``systemd-run --scope`` probe, and asserts the module's paths
and ``genesis_home()`` agree.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Read-only code from this checkout; every other path constant must be in the sandbox.
_CODE_PATHS = {
    "_LIVE_CHECKOUT_SCRIPT": REPO / "scripts" / "lib" / "live_checkout.py",
    "_MANAGED_UNITS_SCRIPT": REPO / "scripts" / "lib" / "managed_units.py",
    "_STATE_GUARD_SCRIPT": REPO / "scripts" / "lib" / "update_state_guard.py",
}


class LaunchRefused(AssertionError):
    """A test reached a real process launch through the routes module."""


def isolate_update_routes(
    monkeypatch,
    home: Path,
    genesis_root: Path | None = None,
    genesis_home: Path | None = None,
):
    """Sandbox the routes module for one test; returns a namespace with
    ``home``, ``genesis_dir``, ``root``, ``launched`` (argv of every refused
    launch) and ``originals`` (the stubbed functions, for tests of them)."""
    from genesis import env
    from genesis.dashboard.routes import updates

    home = Path(home)
    # GENESIS_HOME as the caller already set it (the UI harness uses the home
    # itself), else the default under HOME; every state path follows it.
    dot = Path(genesis_home) if genesis_home is not None else home / ".genesis"
    dot.mkdir(parents=True, exist_ok=True)
    (home / "tmp").mkdir(exist_ok=True)  # the supervised start writes its script there
    root = Path(genesis_root) if genesis_root is not None else home / "genesis"
    root.mkdir(parents=True, exist_ok=True)
    stub = home / "stub-update.sh"
    stub.write_text("#!/bin/bash\nexit 97\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_HOME", str(dot))

    paths = {
        "_HOME": home,
        "_GENESIS_ROOT": root,
        "_UPDATE_SCRIPT": stub,
        "_DB_PATH": root / "data" / "genesis.db",
        "_FAILURE_FILE": dot / "last_update_failure.json",
        "_UNIT_DIR": home / ".config" / "systemd" / "user",
        "_GENESIS_DIR": dot,
        "_SUMMARY_FILE": dot / "last_update_summary.txt",
        "_ESCALATION_FILE": dot / "update_escalation.txt",
        "_CONFLICT_FILE": dot / "update_conflicts.json",
        "_REFUSED_FILE": dot / "update_refused.json",
        "_STATE_FILE": dot / "update_state.json",
        "_PID_FILE": dot / "update_in_progress.pid",
        **_CODE_PATHS,
    }
    for name, value in paths.items():
        # raising=False only for the code paths: a verify-RED run against a routes
        # module that predates one must fail in the test, not in this fixture.
        monkeypatch.setattr(updates, name, value, raising=name not in _CODE_PATHS)

    launched: list[list[str]] = []

    def _refuse(args, *a, **k):
        launched.append([str(x) for x in (args if isinstance(args, list | tuple) else [args])])
        raise LaunchRefused(f"a test reached a real process launch: {args!r}")

    # The routes module's own view of subprocess, with Popen refused. Patching
    # subprocess.Popen itself would also break subprocess.run everywhere.
    routes_subprocess = types.SimpleNamespace(
        **{n: getattr(subprocess, n) for n in dir(subprocess) if not n.startswith("__")}
    )
    routes_subprocess.Popen = _refuse
    monkeypatch.setattr(updates, "subprocess", routes_subprocess)
    originals = {"_systemd_run_scope_available": updates._systemd_run_scope_available}
    monkeypatch.setattr(updates, "_systemd_run_scope_available", lambda env_=None: False)

    # Every path constant agrees with the reader the watchdog uses …
    assert env.genesis_home() == updates._GENESIS_DIR == updates._STATE_FILE.parent
    assert updates._PID_FILE.parent == env.genesis_home()
    # … and none still names the real install: every path constant (a new one
    # included, which fails here until it is added above) is inside the sandbox.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert home != real_home and root != real_home / "genesis", "the sandbox is the real home"
    for name, value in vars(updates).items():
        if not isinstance(value, Path) or name in _CODE_PATHS:
            continue
        inside = value.is_relative_to(home) or value.is_relative_to(root)
        assert inside, f"updates.{name} is outside the sandbox: {value}"

    return types.SimpleNamespace(
        home=home,
        genesis_dir=dot,
        root=root,
        stub=stub,
        launched=launched,
        originals=originals,
    )


def scratch_repo(path: Path) -> Path:
    """An empty git repository with one commit, for ``_GENESIS_ROOT``."""
    path.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return path
