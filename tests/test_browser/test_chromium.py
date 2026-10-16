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
    ("build", "sub", "exe", "machine"),
    [
        ("chromium", "chrome-linux64", "chrome", "x86_64"),
        ("chromium", "chrome-linux", "chrome", "aarch64"),
        (
            "chromium-headless-shell",
            "chrome-headless-shell-linux64",
            "chrome-headless-shell",
            "x86_64",
        ),
        ("chromium-headless-shell", "chrome-linux", "headless_shell", "aarch64"),
    ],
)
def test_a_complete_build_is_ready(tmp_path, monkeypatch, build, sub, exe, machine):
    monkeypatch.setattr(chromium.platform, "machine", lambda: machine)
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


# ── the launches' readiness checks ────────────────────────────────────────────


def test_package_dir_never_raises(monkeypatch):
    """A stubbed module with no spec, or no module at all, reads as absent."""
    assert chromium.package_dir("no_such_package_xyz") is None
    monkeypatch.setitem(sys.modules, "stubbed_pkg_xyz", object())
    assert chromium.package_dir("stubbed_pkg_xyz") is None


def test_the_fallback_is_judged_where_the_driver_launches_from(tmp_path):
    """Playwright's executable_path is <install dir>/<platform dir>/<executable>; the
    build is judged at that install directory, so a host override that moves it
    (revisionOverrides, PLAYWRIGHT_BROWSERS_PATH) is the package's answer, not ours."""
    d = _build(tmp_path, "chrome-linux64", "chrome")
    assert chromium.not_ready_at("patchright", "chromium", d / "chrome-linux64" / "chrome") is None


@pytest.mark.parametrize("marker", [True, False])
def test_an_incomplete_fallback_names_the_package_and_the_stack_script(
    tmp_path, monkeypatch, marker
):
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path / "repo"))
    d = _build(tmp_path, "chrome-linux64", None, marker=marker)
    detail = chromium.not_ready_at("playwright", "chromium", d / "chrome-linux64" / "chrome")
    assert detail.startswith("playwright's chromium at ")
    assert str(tmp_path / "repo" / "scripts" / "install_browser_stack.sh") in detail


def _dry_run_into(tmp_path):
    return DRY_RUN.replace("/cache/ms-playwright", str(tmp_path / "ms-playwright"))


def _install(tmp_path, name: str, sub: str, exe: str) -> None:
    d = tmp_path / "ms-playwright" / name
    (d / sub).mkdir(parents=True)
    (d / sub / exe).write_bytes(b"\x7fELF")
    (d / sub / exe).chmod(0o755)
    (d / "INSTALLATION_COMPLETE").write_text("")


def test_the_cli_asks_the_package_where_its_headless_shell_lives(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path / "repo"))
    run = _run(_dry_run_into(tmp_path))
    _install(tmp_path, "chromium-1234", "chrome-linux64", "chrome")
    detail = chromium.not_ready("patchright", headless=True, run=run)
    assert "chromium-headless-shell at" in detail and "chromium_headless_shell-1234" in detail
    assert run.calls[0][0][-3:] == ["install", "--dry-run", "chromium"]
    _install(
        tmp_path,
        "chromium_headless_shell-1234",
        "chrome-headless-shell-linux64",
        "chrome-headless-shell",
    )
    assert chromium.not_ready("patchright", headless=True, run=run) is None


def test_the_cli_leaves_an_unanswered_layout_to_the_launch(tmp_path):
    assert chromium.not_ready("patchright", run=_run("", rc=1)) is None


def test_browser_version_reads_the_version_a_package_launches(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "driver" / "package").mkdir(parents=True)
    (pkg / "driver" / "package" / "browsers.json").write_text(
        '{"browsers": [{"name": "chromium", "revision": "1234", "browserVersion": "151.0.7922.34"}]}'
    )
    assert chromium.browser_version(pkg) == "151.0.7922.34"
    assert chromium.browser_version(tmp_path / "missing") is None


def test_not_ready_at_checks_the_selected_executable_not_a_sibling(tmp_path):
    """A cache shared across platforms can hold another platform's build beside
    the one Playwright selected."""
    install = tmp_path / "chromium-1234"
    (install / "chrome-linux").mkdir(parents=True)
    other = install / "chrome-linux" / "chrome"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    (install / "INSTALLATION_COMPLETE").write_text("")
    selected = install / "chrome-linux64" / "chrome"
    assert chromium.not_ready_at("patchright", "chromium", selected)


def test_not_ready_at_rejects_a_selected_executable_without_its_exec_bit(tmp_path):
    install = tmp_path / "chromium-1234"
    (install / "chrome-linux64").mkdir(parents=True)
    exe = install / "chrome-linux64" / "chrome"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o644)
    (install / "INSTALLATION_COMPLETE").write_text("")
    assert chromium.not_ready_at("patchright", "chromium", exe)
    exe.chmod(0o755)
    assert chromium.not_ready_at("patchright", "chromium", exe) is None


def test_another_platforms_build_is_not_this_hosts_executable(tmp_path, monkeypatch):
    """Playwright launches only its own platform's path (registry executablePaths);
    a shared cache can hold the other one beside it."""
    monkeypatch.setattr(chromium.platform, "machine", lambda: "x86_64")
    d = _build(tmp_path, "chrome-linux", "chrome")  # the linux-arm64 layout
    assert chromium.executable("chromium", d) is None
    assert chromium.problem("chromium", d)


def test_an_unknown_machine_accepts_any_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(chromium.platform, "machine", lambda: "riscv64")
    d = _build(tmp_path, "chrome-linux64", "chrome")
    assert chromium.executable("chromium", d) == d / "chrome-linux64" / "chrome"
