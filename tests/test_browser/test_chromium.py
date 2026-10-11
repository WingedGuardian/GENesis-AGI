"""Where a Playwright-family package keeps its Chromium, and whether it is there."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from genesis.browser import chromium, engine


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path / "repo"))
    return tmp_path


def _package(env, monkeypatch, *, revision="1234", browsers_json=True):
    pkg = env / "site" / "patchright"
    (pkg / "driver" / "package").mkdir(parents=True)
    if browsers_json:
        (pkg / "driver" / "package" / "browsers.json").write_text(
            json.dumps(
                {
                    "browsers": [
                        {"name": "chromium", "revision": revision},
                        {"name": "chromium-headless-shell", "revision": revision},
                        {"name": "firefox", "revision": "1490"},
                    ]
                }
            )
        )
    monkeypatch.setattr(chromium, "package_dir", lambda name: pkg if name == "patchright" else None)
    return env / "xdg" / "ms-playwright"


def _complete(browsers: Path, name: str) -> None:
    (browsers / name).mkdir(parents=True, exist_ok=True)
    (browsers / name / "INSTALLATION_COMPLETE").touch()


def test_browsers_dir_follows_playwright(env, monkeypatch, tmp_path):
    """registry/index.js: PLAYWRIGHT_BROWSERS_PATH, "0" meaning beside the package,
    else XDG_CACHE_HOME or ~/.cache, plus ms-playwright."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw"))
    assert chromium.browsers_dir(Path("/pkg")) == tmp_path / "pw"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    assert chromium.browsers_dir(Path("/pkg")) == Path("/pkg/driver/package/.local-browsers")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    assert chromium.browsers_dir(Path("/pkg")) == env / "xdg" / "ms-playwright"


def test_install_dirs_names_both_chromium_builds(env, monkeypatch):
    browsers = _package(env, monkeypatch)
    revision, dirs = chromium.install_dirs(chromium.package_dir("patchright"))
    assert revision == "1234"
    assert dirs == {
        "chromium": browsers / "chromium-1234",
        "chromium-headless-shell": browsers / "chromium_headless_shell-1234",
    }


def test_install_dirs_of_an_unreadable_package_is_empty(env, monkeypatch):
    _package(env, monkeypatch, browsers_json=False)
    assert chromium.install_dirs(chromium.package_dir("patchright")) == (None, {})


def test_package_dir_never_raises(monkeypatch):
    """A stubbed module with no spec, or no module at all, reads as absent."""
    assert chromium.package_dir("no_such_package_xyz") is None
    monkeypatch.setitem(sys.modules, "stubbed_pkg_xyz", object())
    assert chromium.package_dir("stubbed_pkg_xyz") is None


def test_ready_when_the_headed_build_finished_installing(env, monkeypatch):
    browsers = _package(env, monkeypatch)
    _complete(browsers, "chromium-1234")  # the headless shell is not what launches
    assert chromium.not_ready("patchright") is None


def test_not_ready_names_the_revision_and_the_stack_script(env, monkeypatch):
    """Playwright's own error tells the reader to run `patchright install`, a
    download sessions must never start; this names the operator step instead."""
    browsers = _package(env, monkeypatch)
    (browsers / "chromium-1234").mkdir(parents=True)  # a killed install: no marker
    detail = chromium.not_ready("patchright")
    assert detail is not None
    assert "1234" in detail and "patchright" in detail
    assert "tell the user" in detail
    assert "scripts/install_browser_stack.sh" in detail
    assert "patchright install" not in detail


def test_plain_playwright_chromium_names_the_browser_extra(env, monkeypatch):
    """The provisioner installs patchright's Chromium only; without patchright the
    repair is installing the browser extra, which brings it."""
    pkg = env / "site" / "playwright"
    (pkg / "driver" / "package").mkdir(parents=True)
    (pkg / "driver" / "package" / "browsers.json").write_text(
        json.dumps({"browsers": [{"name": "chromium", "revision": "1234"}]})
    )
    monkeypatch.setattr(chromium, "package_dir", lambda name: pkg if name == "playwright" else None)
    detail = chromium.not_ready("playwright")
    assert detail is not None and engine.PACKAGE_HINT in detail
    assert "install_browser_stack" not in detail


def test_a_headless_launch_needs_the_headless_shell(env, monkeypatch):
    """Since Playwright 1.49 a headless launch runs chromium-headless-shell, a
    separate build with its own directory; a headed one runs chromium."""
    browsers = _package(env, monkeypatch)
    _complete(browsers, "chromium-1234")
    detail = chromium.not_ready("patchright", headless=True)
    assert detail is not None and "chromium_headless_shell-1234" in detail
    _complete(browsers, "chromium_headless_shell-1234")
    assert chromium.not_ready("patchright", headless=True) is None


def test_an_unreadable_package_is_left_to_the_launch(env, monkeypatch):
    """No browsers.json or no package: the launch's own import or driver error is
    the accurate one, so the check claims nothing."""
    _package(env, monkeypatch, browsers_json=False)
    assert chromium.not_ready("patchright") is None
    assert chromium.not_ready("playwright") is None
