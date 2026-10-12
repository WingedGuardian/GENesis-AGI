"""genesis.browser.packages: refuse a browser launch on modules that no longer
match the files on disk.

pip replacing camoufox/playwright/patchright under a live MCP process leaves the
OLD modules in sys.modules; the new files then fail in ways reinstalling cannot
fix. Only a restart loads them. Carried from #2906 (the per-distribution
``_LOADED``/``_LAST_SEEN`` baseline answered Devin finding 4187451360: a package
first imported after it changed on disk loaded the NEW files and is current).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from genesis.browser import packages


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    """Each test sets its own startup snapshot; nothing carries between tests."""

    def set_startup(**versions):
        startup = {d: versions.get(d) for d in packages.BROWSER_DISTS}
        monkeypatch.setattr(packages, "STARTUP_VERSIONS", startup)
        monkeypatch.setattr(packages, "_loaded", {})
        monkeypatch.setattr(packages, "_last_seen", dict(startup))
        return startup

    return set_startup


def _modules(loaded=()):
    """sys.modules minus every browser distribution, plus fakes for ``loaded``."""
    mods = {k: v for k, v in sys.modules.items() if k.split(".")[0] not in packages.BROWSER_DISTS}
    for name in loaded:
        mods[name] = MagicMock()
    return mods


def test_unchanged_loaded_package_passes(_baseline):
    startup = _baseline(camoufox="0.5.7", playwright="1.62.0")
    with (
        patch.object(packages, "installed_versions", return_value=dict(startup)),
        patch.dict("sys.modules", _modules(["camoufox", "playwright"]), clear=True),
    ):
        packages.check_loaded_browser_modules()
        packages.check_loaded_browser_modules()


def test_package_installed_then_imported_is_not_stale(_baseline):
    startup = _baseline(camoufox="0.4.11")
    upgraded = dict(startup, camoufox="0.5.7")
    with (
        patch.object(packages, "installed_versions", return_value=upgraded),
        patch.dict("sys.modules", _modules(), clear=True),
    ):
        packages.check_loaded_browser_modules()  # nothing loaded: fine
        sys.modules["camoufox"] = MagicMock()  # the launch imports 0.5.7
        packages.check_loaded_browser_modules()
        packages.check_loaded_browser_modules()


def test_import_across_a_change_is_stale(_baseline):
    """Loaded while the disk changed under it: which version is unknown."""
    startup = _baseline(camoufox="0.4.11")
    with (
        patch.object(packages, "installed_versions", return_value=dict(startup, camoufox="0.5.7")),
        patch.dict("sys.modules", _modules(["camoufox"]), clear=True),
        pytest.raises(packages.BrowserPackagesChanged, match="Restart"),
    ):
        packages.check_loaded_browser_modules()


def test_change_after_the_import_is_stale(_baseline):
    startup = _baseline(camoufox="0.5.7")
    disk = {"now": dict(startup)}
    with (
        patch.object(packages, "installed_versions", lambda: dict(disk["now"])),
        patch.dict("sys.modules", _modules(["camoufox"]), clear=True),
    ):
        packages.check_loaded_browser_modules()  # loaded at 0.5.7: fine
        disk["now"] = dict(startup, camoufox="0.6.0")
        with pytest.raises(packages.BrowserPackagesChanged, match="0.5.7 -> 0.6.0"):
            packages.check_loaded_browser_modules()


def test_an_unloaded_package_change_needs_no_restart(_baseline):
    """camoufox changed on disk while only playwright is loaded: Chromium may launch."""
    startup = _baseline(camoufox="0.4.11", playwright="1.58.0")
    with (
        patch.object(packages, "installed_versions", return_value=dict(startup, camoufox="9.9.9")),
        patch.dict("sys.modules", _modules(["playwright"]), clear=True),
    ):
        packages.check_loaded_browser_modules()


def test_a_stub_module_with_no_distribution_is_not_stale(_baseline):
    """Tests stub playwright in sys.modules where it is not installed (CI): the
    distribution is absent before and after, so nothing changed."""
    _baseline()
    with (
        patch.object(
            packages, "installed_versions", return_value={d: None for d in packages.BROWSER_DISTS}
        ),
        patch.dict("sys.modules", _modules(["playwright", "playwright.async_api"]), clear=True),
    ):
        packages.check_loaded_browser_modules()
        packages.check_loaded_browser_modules()


def test_a_module_no_longer_loaded_is_not_stale(_baseline):
    """A stub that a test's patch.dict put in sys.modules and then removed was
    recorded by an earlier check; once it is gone it cannot be stale."""
    startup = _baseline(playwright="1.58.0")
    disk = {"now": dict(startup)}
    with patch.object(packages, "installed_versions", lambda: dict(disk["now"])):
        with patch.dict("sys.modules", _modules(["playwright"]), clear=True):
            packages.check_loaded_browser_modules()  # records playwright 1.58.0
        disk["now"] = dict(startup, playwright="1.62.0")
        with patch.dict("sys.modules", _modules(), clear=True):
            packages.check_loaded_browser_modules()  # no longer loaded: fine


def test_restart_message_names_versions(_baseline):
    _baseline(playwright="1.58.0")
    with patch.object(
        packages,
        "installed_versions",
        return_value={"camoufox": None, "playwright": "1.62.0", "patchright": None},
    ):
        message = packages.packages_changed_message(["playwright"])
    assert "playwright 1.58.0 -> 1.62.0" in message
    assert "Restart this Claude Code session" in message


def test_import_error_after_an_upgrade_says_restart(_baseline):
    """playwright 1.58 stays loaded while 1.62 lands on disk; new camoufox code then
    imports a name the loaded playwright lacks. Only a restart fixes that."""
    _baseline(playwright="1.58.0")
    with (
        patch.object(
            packages,
            "installed_versions",
            return_value={"camoufox": None, "playwright": "1.62.0", "patchright": None},
        ),
        patch.dict("sys.modules", _modules(["playwright"]), clear=True),
    ):
        message = packages.import_error_message(
            ImportError("cannot import name 'BrowserBindResult'")
        )
    assert "Restart this Claude Code session" in message
    assert "BrowserBindResult" in message
    assert "camoufox fetch" not in message


def test_import_error_after_an_unloaded_change_needs_no_restart(_baseline):
    """patchright installed during the session, nothing browser-side loaded: the
    failed import read the files on disk, so a restart would read the same ones."""
    startup = _baseline(camoufox="0.5.8", playwright="1.62.0")
    with (
        patch.object(
            packages, "installed_versions", return_value=dict(startup, patchright="1.62.3")
        ),
        patch.dict("sys.modules", _modules(), clear=True),
    ):
        message = packages.import_error_message(ImportError("No module named 'camoufox'"))
        stale = packages.stale_loaded_modules()
    assert message.startswith("Browser not available")
    assert "Restart" not in message
    assert stale == []


def test_stale_loaded_modules_names_only_loaded_changed_ones(_baseline):
    startup = _baseline(camoufox="0.5.7", playwright="1.62.0")
    with (
        patch.object(
            packages,
            "installed_versions",
            return_value=dict(startup, camoufox="0.5.8", playwright="1.62.1", patchright="1.62.3"),
        ),
        patch.dict("sys.modules", _modules(["playwright"]), clear=True),
    ):
        packages._loaded["playwright"] = "1.62.0"
        assert packages.stale_loaded_modules() == ["playwright"]


def test_import_error_without_an_upgrade_says_tell_the_user(_baseline):
    startup = _baseline(playwright="1.58.0")
    with patch.object(packages, "installed_versions", return_value=dict(startup)):
        message = packages.import_error_message(ImportError("No module named 'camoufox'"))
    assert message.startswith("Browser not available")
    assert "tell the user" in message
    assert "pip install" not in message.split("operator step")[0]


def test_baseline_is_taken_at_import_without_browser_packages(tmp_path):
    """The module imports cleanly with neither package importable, and snapshots
    the on-disk versions at import (before scrapling can pull playwright in)."""
    src = str(Path(packages.__file__).resolve().parents[2])
    code = (
        "import sys; sys.modules['camoufox'] = None; sys.modules['playwright'] = None\n"
        "from genesis.browser import packages\n"
        "assert set(packages.STARTUP_VERSIONS) == set(packages.BROWSER_DISTS)\n"
        "assert 'camoufox' not in packages._loaded\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_a_layer_checks_only_the_packages_it_imports(_baseline):
    """A camoufox-only upgrade must not refuse the playwright-only layers
    (Chromium, remote CDP, TinyFish)."""
    startup = _baseline(camoufox="0.5.7", playwright="1.62.0")
    disk = {"now": dict(startup)}
    with (
        patch.object(packages, "installed_versions", lambda: dict(disk["now"])),
        patch.dict("sys.modules", _modules(["camoufox", "playwright"]), clear=True),
    ):
        packages.check_loaded_browser_modules()  # both loaded at their startup versions
        disk["now"] = dict(startup, camoufox="0.5.8")
        packages.check_loaded_browser_modules(packages.PLAYWRIGHT_ONLY)
        with pytest.raises(packages.BrowserPackagesChanged, match="0.5.7 -> 0.5.8"):
            packages.check_loaded_browser_modules()


def test_an_uninstalled_package_is_not_told_to_skip_reinstalling(_baseline):
    _baseline(camoufox="0.5.7")
    with patch.object(
        packages,
        "installed_versions",
        return_value={"camoufox": None, "playwright": None, "patchright": None},
    ):
        message = packages.packages_changed_message(["camoufox"])
    assert "camoufox 0.5.7 -> None" in message
    assert "do not reinstall" not in message
    assert "tell the user" in message


@pytest.mark.parametrize(
    "exc", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), OSError(5, "EIO")]
)
def test_unreadable_metadata_is_reported_not_raised(monkeypatch, exc):
    """importlib.metadata raises these for a damaged .dist-info; the launch guard
    must not let them escape before the import-failure handling runs."""

    def version(name):
        if name == "camoufox":
            raise exc
        raise packages.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(packages.metadata, "version", version)
    assert packages.installed_versions() == {
        "camoufox": packages.UNREADABLE,
        "playwright": None,
        "patchright": None,
    }


def test_metadata_damaged_after_startup_says_reinstall(_baseline):
    _baseline(camoufox="0.5.7")
    with patch.object(
        packages,
        "installed_versions",
        return_value={"camoufox": packages.UNREADABLE, "playwright": None, "patchright": None},
    ):
        message = packages.packages_changed_message(["camoufox"])
    assert "do not reinstall" not in message
    assert packages.PACKAGE_HINT in message


def test_import_error_advice_is_scoped_to_the_failing_layer(_baseline):
    """A stale camoufox is no reason to restart for a failed Chromium import."""
    startup = _baseline(camoufox="0.5.7", playwright="1.62.0")
    with (
        patch.object(packages, "installed_versions", return_value=dict(startup, camoufox="0.5.8")),
        patch.dict("sys.modules", _modules(["camoufox", "playwright"]), clear=True),
    ):
        packages._loaded.update(camoufox="0.5.7", playwright="1.62.0")
        chromium = packages.import_error_message(ImportError("x"), packages.PLAYWRIGHT_ONLY)
        camoufox = packages.import_error_message(ImportError("x"), packages.CAMOUFOX_DISTS)
    assert chromium.startswith("Browser not available")
    assert "Restart this Claude Code session" in camoufox


def test_camoufox_dists_leave_out_the_chromium_package():
    assert set(packages.CAMOUFOX_DISTS) == {"camoufox", "playwright"}
