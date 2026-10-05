"""scripts/browser.py is a local browser too: it holds the browser-stack lock.

The CLI launches Chromium on the shared ~/.genesis/browser-profile, so it is one
of the processes a provisioning run must not swap packages or copy a profile
under. It imports no genesis modules, so it carries its own copy of the lock
path; this test keeps the copy equal to genesis.browser.engine's.
"""

from __future__ import annotations

import fcntl
import os
import sys

import pytest

from genesis.browser import engine
from tests.test_scripts.test_browser_cli_screenshot_path import _load_cli


@pytest.fixture
def cli(tmp_path, monkeypatch):
    module = _load_cli()
    monkeypatch.setattr(module, "STACK_LOCK_FILE", tmp_path / "locks" / "browser.lock", raising=False)
    yield module
    fd = getattr(module, "_stack_lock_fd", None)
    if fd is not None:
        os.close(fd)


def test_lock_path_matches_the_engine_module():
    module = _load_cli()
    assert getattr(module, "STACK_LOCK_FILE", None) == engine.BROWSER_LOCK_FILE


def test_command_refused_during_an_upgrade(cli, tmp_path, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli, "cmd_snapshot", lambda args: ran.append(args))
    monkeypatch.setattr(sys, "argv", ["browser.py", "snapshot"])
    cli.STACK_LOCK_FILE.parent.mkdir(parents=True)
    with open(cli.STACK_LOCK_FILE, "w") as provisioning:
        fcntl.flock(provisioning, fcntl.LOCK_EX)
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
    assert exit_info.value.code == 1
    assert "being upgraded" in capsys.readouterr().err
    assert ran == []


def test_command_runs_under_a_shared_hold(cli, monkeypatch):
    held = []

    def probe(_args):
        with open(cli.STACK_LOCK_FILE, "w") as provisioning:
            try:
                fcntl.flock(provisioning, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)

    monkeypatch.setattr(cli, "cmd_snapshot", probe)
    monkeypatch.setattr(sys, "argv", ["browser.py", "snapshot"])
    cli.main()
    assert held == [True]


def test_playwright_is_imported_only_under_the_hold(cli, monkeypatch, capsys):
    """The import reads package files a provisioning run replaces: it must come
    after the lock, so a refused command never imports playwright at all."""
    imported = []
    monkeypatch.setattr(cli, "_sync_playwright", lambda: imported.append(True), raising=False)
    monkeypatch.setattr(sys, "argv", ["browser.py", "snapshot"])
    cli.STACK_LOCK_FILE.parent.mkdir(parents=True)
    with open(cli.STACK_LOCK_FILE, "w") as provisioning:
        fcntl.flock(provisioning, fcntl.LOCK_EX)
        with pytest.raises(SystemExit):
            cli.main()
    assert imported == []
    assert "sync_playwright" not in vars(cli)  # no module-scope import either
