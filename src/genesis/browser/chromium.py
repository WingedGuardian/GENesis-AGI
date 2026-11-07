"""The Chromium the fallback launches: which package, where it lives, whether it is
complete.

The provisioner installs what the browser tools launch by applying the same rule
as the runtime's Chromium fallback: patchright when its distribution is installed,
otherwise playwright. Where a package installs Chromium is asked of the package itself
(``install --dry-run``), which applies its own host-specific revision overrides and
``PLAYWRIGHT_BROWSERS_PATH``; nothing here recomputes Playwright's registry. A
build counts as installed only when Playwright's completion marker AND its
executable are there: the installer skips any directory that has the marker, so a
lost executable is never repaired by a plain install. The launches refuse with
the provisioning step while their build is not complete (``not_ready_at``,
``not_ready``); the profile copy reads the version a package launches.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from genesis.browser import packages
from genesis.browser.engine import stack_step

# The two builds `install chromium` writes. A headed launch runs chromium, a
# headless one (Playwright >= 1.49) chromium-headless-shell.
BUILDS = ("chromium", "chromium-headless-shell")
# Each build's executable on Linux, per Playwright's registry (EXECUTABLE_PATHS):
# chromium is <dir>/chrome on x64 and arm64; the headless shell is
# chrome-headless-shell on x64 and headless_shell on arm64.
_EXECUTABLES = {
    "chromium": ("chrome",),
    "chromium-headless-shell": ("chrome-headless-shell", "headless_shell"),
}
_MARKER = "INSTALLATION_COMPLETE"
# Starts the package's Node driver and prints; MEASURED 1-2 s. Generous for a slow
# disk, and short enough that a wedged driver cannot hold the oneshot.
_DRY_RUN_TIMEOUT_S = 600
# Every browser's header, so one build's location can never be read as another's.
_LABEL = re.compile(r"\((?:playwright|patchright) (\S+) v(\S+)\)\s*$")
_LOCATION = re.compile(r"^\s+Install location:\s+(/.*?)\s*$")


class ChromiumLayoutUnknown(RuntimeError):
    """The package did not say where it installs Chromium."""


def launch_package() -> str | None:
    """The package the Chromium fallback imports: patchright whenever its
    distribution is installed (a damaged one too, so its error surfaces), else
    playwright; None with neither."""
    versions = packages.installed_versions()
    if versions.get("patchright") is not None:
        return "patchright"
    return "playwright" if versions.get("playwright") is not None else None


def locations(
    package: str, *, run: Callable[..., subprocess.CompletedProcess] | None = None
) -> dict[str, tuple[str, Path]]:
    """``{build: (revision, install directory)}`` for both builds, from
    ``python -m <package> install --dry-run chromium``. Raises ChromiumLayoutUnknown
    when the answer does not name both."""
    run = run or subprocess.run
    argv = [sys.executable, "-m", package, "install", "--dry-run", "chromium"]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=_DRY_RUN_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChromiumLayoutUnknown(f"{package} install --dry-run did not run: {exc!r}") from exc
    found: dict[str, tuple[str, Path]] = {}
    label: tuple[str, str] | None = None
    for line in (proc.stdout or "").splitlines():
        if m := _LABEL.search(line):
            label = (m.group(1), m.group(2))
        elif label and (m := _LOCATION.match(line)):
            if label[0] in BUILDS:
                found[label[0]] = (label[1], Path(m.group(1)))
            label = None
    if proc.returncode != 0 or set(found) != set(BUILDS):
        first = next(iter((proc.stdout or proc.stderr or "").splitlines()), "")
        raise ChromiumLayoutUnknown(
            f"{package} install --dry-run chromium (exit {proc.returncode}) named "
            f"{sorted(found) or 'no build'}; its first line: {first[:200]!r}"
        )
    return found


# Where Playwright launches each build from, per machine (its registry's
# executablePaths for linux-x64 / linux-arm64; patchright 1.62.3 coreBundle.js,
# READ 2026-10-11). Only that path counts: a cache shared across machines can hold
# the other platform's build beside it. An unlisted machine accepts any layout.
_SELECTED = {
    "x86_64": {
        "chromium": "chrome-linux64/chrome",
        "chromium-headless-shell": "chrome-headless-shell-linux64/chrome-headless-shell",
    },
    "aarch64": {
        "chromium": "chrome-linux/chrome",
        "chromium-headless-shell": "chrome-linux/headless_shell",
    },
}


def executable(build: str, directory: Path) -> Path | None:
    """The build's executable inside its install directory, or None."""
    selected = _SELECTED.get(platform.machine(), {}).get(build)
    if selected is not None:
        candidate = Path(directory) / selected
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
    for name in _EXECUTABLES[build]:
        for candidate in sorted(Path(directory).glob(f"*/{name}")):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def needs_force(build: str, directory: Path) -> bool:
    """Marked complete with its executable gone: a plain install skips it, so only
    ``install --force`` repairs it."""
    return (Path(directory) / _MARKER).is_file() and executable(build, directory) is None


def problem(build: str, directory: Path) -> str | None:
    """Why ``build`` in ``directory`` cannot launch, or None."""
    directory = Path(directory)
    if not (directory / _MARKER).is_file():
        return f"{build} at {directory} is not installed (its install has not finished)"
    if executable(build, directory) is None:
        return f"{build} at {directory} has no executable (marked complete, files missing)"
    return None


def not_ready_at(package: str, build: str, executable_path: str | Path) -> str | None:
    """Why ``build`` cannot launch, judged where the launching package itself puts
    its executable (Playwright's ``browser_type.executable_path``, which applies its
    host overrides and PLAYWRIGHT_BROWSERS_PATH), or None. The install directory is
    two levels up: ``<install dir>/<platform dir>/<executable>``. The selected
    executable itself must be there and executable: a cache shared across platforms
    can hold another platform's build beside it."""
    exe = Path(executable_path)
    directory = exe.parent.parent
    found = None
    if not (directory / _MARKER).is_file():
        found = f"{build} at {directory} is not installed (its install has not finished)"
    elif not (exe.is_file() and os.access(exe, os.X_OK)):
        found = f"{build} at {directory} has no launchable {exe.name} where {package} looks"
    return f"{package}'s {found}; {stack_step()}" if found else None


def not_ready(
    package: str,
    *,
    headless: bool = False,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str | None:
    """``not_ready_at`` for a launch with no driver started yet (the CLI): the
    package's own dry-run says where the build lives. A package that cannot say is
    left to the launch, whose own error is the accurate one."""
    try:
        where = locations(package, run=run)
    except ChromiumLayoutUnknown:
        return None
    build = "chromium-headless-shell" if headless else "chromium"
    found = problem(build, where[build][1])
    return f"{package}'s {found}; {stack_step()}" if found else None


class ChromiumNotReady(RuntimeError):
    """The Chromium fallback's browser is not installed; the message is the repair."""


def package_dir(name: str) -> Path | None:
    """Where the installed package ``name`` lives, or None. Never raises: a stub or
    broken module reads as absent."""
    try:
        spec = importlib.util.find_spec(name)
        origin = spec.origin if spec is not None else None
    except Exception:  # noqa: BLE001 - a broken package reads as absent
        return None
    return Path(origin).parent if origin else None


def browser_version(package: Path) -> str | None:
    """The Chromium version ``package`` launches (its ``driver/package/browsers.json``
    ``chromium`` entry's ``browserVersion``), the string a Chromium profile records;
    None when unreadable. A version, not a location: no host override changes it."""
    try:
        data = json.loads((Path(package) / "driver" / "package" / "browsers.json").read_text())
        for entry in data.get("browsers", []):
            if entry.get("name") == "chromium":
                return str(entry.get("browserVersion") or "").strip() or None
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return None
