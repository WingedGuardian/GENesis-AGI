"""The Chromium the fallback launches: which package, where it lives, whether it is
complete.

The provisioner installs what the browser tools launch by applying the same rule
as the runtime's Chromium fallback: patchright when its distribution is installed,
otherwise playwright. Where a package installs Chromium is asked of the package itself
(``install --dry-run``), which applies its own host-specific revision overrides and
``PLAYWRIGHT_BROWSERS_PATH``; nothing here recomputes Playwright's registry. A
build counts as installed only when Playwright's completion marker AND its
executable are there: the installer skips any directory that has the marker, so a
lost executable is never repaired by a plain install.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from genesis.browser import packages

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


def executable(build: str, directory: Path) -> Path | None:
    """The build's executable inside its install directory, or None."""
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
