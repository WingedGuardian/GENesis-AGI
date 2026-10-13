"""scripts/browser.py drives Chromium through patchright when it is installed, and
copies the profile aside first when a different Chromium is about to open it."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_CLI = Path(__file__).resolve().parents[2] / "scripts" / "browser.py"


@pytest.fixture
def cli(monkeypatch):
    spec = importlib.util.spec_from_file_location("browser_cli_under_test", _CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # imports no browser package at module level
    return mod


def _package(monkeypatch, name):
    manager = MagicMock(name=f"{name}-manager")
    root = types.ModuleType(name)
    sync_api = types.ModuleType(f"{name}.sync_api")
    sync_api.sync_playwright = MagicMock(return_value=manager)
    root.sync_api = sync_api
    monkeypatch.setitem(sys.modules, name, root)
    monkeypatch.setitem(sys.modules, f"{name}.sync_api", sync_api)
    return manager


def test_the_cli_imports_no_browser_package(cli):
    assert "sync_playwright" not in vars(cli)


def test_patchright_is_used_when_installed(cli, monkeypatch):
    monkeypatch.setattr(cli, "_installed", lambda dist: dist == "patchright")
    manager = _package(monkeypatch, "patchright")
    _package(monkeypatch, "playwright")
    assert cli._sync_playwright() == (manager, "patchright")


def test_playwright_without_patchright(cli, monkeypatch):
    monkeypatch.setattr(cli, "_installed", lambda dist: False)
    manager = _package(monkeypatch, "playwright")
    assert cli._sync_playwright() == (manager, "playwright")


def test_a_broken_patchright_raises(cli, monkeypatch):
    monkeypatch.setattr(cli, "_installed", lambda dist: dist == "patchright")
    monkeypatch.setitem(sys.modules, "patchright", None)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", None)
    _package(monkeypatch, "playwright")
    with pytest.raises(ImportError):
        cli._sync_playwright()


def test_open_copies_the_profile_for_the_package_that_launches(cli, monkeypatch):
    from genesis.browser import chromium, profile_backup

    monkeypatch.setattr(chromium, "not_ready", lambda package, **kw: None)
    monkeypatch.setattr(cli, "_installed", lambda dist: dist == "patchright")
    manager = _package(monkeypatch, "patchright")
    backup = MagicMock()
    monkeypatch.setattr(profile_backup, "backup_chromium_profile", backup)
    assert cli._open() is manager
    backup.assert_called_once_with(cli.USER_DATA_DIR, "patchright")


def test_open_without_genesis_makes_no_copy(cli, monkeypatch):
    monkeypatch.setattr(cli, "_installed", lambda dist: False)
    manager = _package(monkeypatch, "playwright")
    monkeypatch.setitem(sys.modules, "genesis.browser.profile_backup", None)
    assert cli._open() is manager


def test_open_refuses_a_missing_chromium_before_anything_starts(cli, monkeypatch, capsys):
    """Playwright's own error would tell the reader to run `patchright install`;
    the CLI names the operator step instead, as the MCP fallback does."""
    from genesis.browser import chromium, profile_backup

    monkeypatch.setattr(cli, "_installed", lambda dist: dist == "patchright")
    manager = _package(monkeypatch, "patchright")
    asked = []

    def not_ready(package, **kw):
        asked.append(kw)
        return f"{package} missing; STEP"

    monkeypatch.setattr(chromium, "not_ready", not_ready)
    backup = MagicMock()
    monkeypatch.setattr(profile_backup, "backup_chromium_profile", backup)
    with pytest.raises(SystemExit) as exit_:
        cli._open()
    assert exit_.value.code == 1
    assert "patchright missing; STEP" in capsys.readouterr().err
    backup.assert_not_called()
    manager.start.assert_not_called()
    assert asked == [{"headless": True}]  # the CLI launches headless: the headless shell
