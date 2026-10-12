"""genesis.browser.chromium: which Playwright-family package the Chromium fallback
launches, where that package installs Chromium (asked of the package itself), and
whether an installed build is complete."""

from __future__ import annotations

import subprocess
import sys

import pytest

from genesis.browser import chromium

# `python -m patchright install --dry-run chromium` (1.62.3), paths made generic.
DRY_RUN = """\
Chrome for Testing 151.0.7922.34 (playwright chromium v1234)
  Install location:    /cache/ms-playwright/chromium-1234
  Download url:        https://cdn.playwright.dev/builds/cft/151.0.7922.34/linux64/chrome-linux64.zip

FFmpeg (playwright ffmpeg v1011)
  Install location:    /cache/ms-playwright/ffmpeg-1011
  Download url:        https://cdn.playwright.dev/dbazure/download/playwright/builds/ffmpeg/1011/ffmpeg-linux.zip

FFmpeg (playwright ffmpeg v1011)
  Install location:    /cache/ms-playwright/ffmpeg-1011

Chrome Headless Shell 151.0.7922.34 (playwright chromium-headless-shell v1234)
  Install location:    /cache/ms-playwright/chromium_headless_shell-1234
  Download url:        https://cdn.playwright.dev/builds/cft/151.0.7922.34/linux64/chrome-headless-shell-linux64.zip
"""


def _run(stdout: str, rc: int = 0):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    run.calls = calls
    return run


# ── which package ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("versions", "expected"),
    [
        ({"playwright": "1.62.0", "patchright": "1.62.3"}, "patchright"),
        ({"playwright": "1.62.0", "patchright": None}, "playwright"),
        ({"playwright": None, "patchright": None}, None),
        # A damaged patchright is still the one the fallback imports (and reports).
        ({"playwright": "1.62.0", "patchright": "unreadable metadata"}, "patchright"),
    ],
)
def test_launch_package_is_patchright_whenever_it_is_installed(monkeypatch, versions, expected):
    monkeypatch.setattr(
        chromium.packages, "installed_versions", lambda: dict(versions, camoufox=None)
    )
    assert chromium.launch_package() == expected


# ── where it installs ─────────────────────────────────────────────────────────


def test_locations_come_from_the_packages_own_dry_run():
    run = _run(DRY_RUN)
    found = chromium.locations("patchright", run=run)
    assert found == {
        "chromium": ("1234", chromium.Path("/cache/ms-playwright/chromium-1234")),
        "chromium-headless-shell": (
            "1234",
            chromium.Path("/cache/ms-playwright/chromium_headless_shell-1234"),
        ),
    }
    ((argv, kw),) = run.calls
    assert argv == [sys.executable, "-m", "patchright", "install", "--dry-run", "chromium"]
    assert kw["timeout"] <= 600 and kw["check"] is False


def test_a_patchright_label_is_read_too():
    found = chromium.locations(
        "patchright", run=_run(DRY_RUN.replace("(playwright ", "(patchright "))
    )
    assert set(found) == {"chromium", "chromium-headless-shell"}


@pytest.mark.parametrize(
    ("stdout", "rc"),
    [
        (DRY_RUN, 1),
        ("", 0),
        (DRY_RUN.replace("chromium-headless-shell v1234", "something-else v1"), 0),
        (DRY_RUN.replace("  Install location:    /cache/ms-playwright/chromium-1234\n", ""), 0),
        (DRY_RUN.replace("/cache/ms-playwright/chromium-1234", "<system>"), 0),
    ],
)
def test_an_answer_that_does_not_name_both_builds_is_unknown(stdout, rc):
    with pytest.raises(chromium.ChromiumLayoutUnknown) as exc:
        chromium.locations("playwright", run=_run(stdout, rc))
    assert "dry-run" in str(exc.value)


def test_a_dry_run_that_cannot_start_is_unknown():
    def run(argv, **kw):
        raise OSError("no python")

    with pytest.raises(chromium.ChromiumLayoutUnknown):
        chromium.locations("playwright", run=run)


# ── is a build complete ───────────────────────────────────────────────────────


def _build(tmp_path, sub: str, exe: str | None, *, marker=True, mode=0o755):
    d = tmp_path / "b"
    (d / sub).mkdir(parents=True)
    if exe:
        (d / sub / exe).write_bytes(b"\x7fELF")
        (d / sub / exe).chmod(mode)
    if marker:
        (d / "INSTALLATION_COMPLETE").write_text("")
    return d


@pytest.mark.parametrize(
    ("build", "sub", "exe"),
    [
        ("chromium", "chrome-linux64", "chrome"),  # linux-x64
        ("chromium", "chrome-linux", "chrome"),  # linux-arm64
        ("chromium-headless-shell", "chrome-headless-shell-linux64", "chrome-headless-shell"),
        ("chromium-headless-shell", "chrome-linux", "headless_shell"),  # linux-arm64
    ],
)
def test_a_complete_build_is_ready(tmp_path, build, sub, exe):
    d = _build(tmp_path, sub, exe)
    assert chromium.problem(build, d) is None
    assert chromium.executable(build, d) == d / sub / exe


def test_a_marker_without_its_executable_is_not_ready(tmp_path):
    """Playwright's installer skips a directory that has its marker, so this state
    never repairs itself (coreBundle.js: the marker check returns early)."""
    d = _build(tmp_path, "chrome-linux64", None)
    assert "no executable" in chromium.problem("chromium", d)


def test_another_builds_executable_does_not_count(tmp_path):
    d = _build(tmp_path, "chrome-linux64", "chrome")
    assert chromium.problem("chromium-headless-shell", d) is not None


def test_an_executable_without_the_marker_is_not_ready(tmp_path):
    d = _build(tmp_path, "chrome-linux64", "chrome", marker=False)
    assert "not finished" in chromium.problem("chromium", d)


def test_a_non_executable_file_is_not_the_executable(tmp_path):
    d = _build(tmp_path, "chrome-linux64", "chrome", mode=0o644)
    assert chromium.problem("chromium", d) is not None
