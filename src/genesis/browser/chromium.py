"""Where a Playwright-family package (playwright, patchright) keeps its Chromium,
and whether that Chromium is installed.

One reading of Playwright's own install layout, shared by the provisioner (which
confirms an install), the profile copy (which reads the version a package
launches) and the Chromium fallback's launch check.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from genesis.browser.engine import PACKAGE_HINT, stack_step

# The builds `install chromium` writes; the headed one is what the fallback launches.
_BUILDS = ("chromium", "chromium-headless-shell")


def package_dir(name: str) -> Path | None:
    """Where the installed package ``name`` lives, or None. Never raises: a stub or
    broken module reads as absent."""
    try:
        spec = importlib.util.find_spec(name)
        origin = spec.origin if spec is not None else None
    except Exception:  # noqa: BLE001 - a broken package reads as absent
        return None
    return Path(origin).parent if origin else None


def browsers_dir(package: Path) -> Path:
    """Where a Playwright-family package installs browsers (its
    ``registry/index.js``): PLAYWRIGHT_BROWSERS_PATH, "0" meaning beside the
    package, else ``$XDG_CACHE_HOME`` or ``~/.cache``, plus ``ms-playwright``."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if env == "0":
        return Path(package) / "driver" / "package" / ".local-browsers"
    if env:
        return Path(env).absolute()
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "ms-playwright"


def _browsers_json(package: Path) -> list:
    """The ``browsers`` list of a package's ``driver/package/browsers.json``."""
    data = json.loads((Path(package) / "driver" / "package" / "browsers.json").read_text())
    return list(data.get("browsers", []))


def browser_version(package: Path) -> str | None:
    """The Chromium version ``package`` launches (its ``chromium`` entry's
    ``browserVersion``), the string a Chromium profile records; None when unreadable."""
    try:
        for entry in _browsers_json(package):
            if entry.get("name") == "chromium":
                return str(entry.get("browserVersion") or "").strip() or None
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return None


def install_dirs(package: Path) -> tuple[str | None, dict[str, Path]]:
    """The Chromium revision ``package`` launches and the directory each Chromium
    build installs to (``<name_with_underscores>-<revision>``), from its
    ``driver/package/browsers.json``; ``(None, {})`` when that is unreadable."""
    try:
        entries = [e for e in _browsers_json(package) if e.get("name") in _BUILDS]
    except (OSError, ValueError, AttributeError, TypeError):
        return None, {}
    root = browsers_dir(package)
    revision, dirs = None, {}
    for entry in entries:
        rev = str(entry.get("revision", ""))
        dirs[entry["name"]] = root / f"{entry['name'].replace('-', '_')}-{rev}"
        if entry["name"] == "chromium":
            revision = rev
    return revision, dirs


def not_ready(package: str, *, headless: bool = False) -> str | None:
    """Why the Chromium ``package`` launches is not installed, or None. Since
    Playwright 1.49 a headless launch runs the separate chromium-headless-shell
    build. Only a missing ``INSTALLATION_COMPLETE`` marker counts (Playwright
    writes it last): an unreadable package is left to the launch, whose own error
    is the accurate one."""
    location = package_dir(package)
    revision, dirs = install_dirs(location) if location else (None, {})
    build = dirs.get("chromium-headless-shell" if headless else "chromium")
    if revision is None or build is None or (build / "INSTALLATION_COMPLETE").is_file():
        return None
    # The provisioner installs patchright's Chromium only; without patchright, the
    # repair is the browser extra, which brings patchright.
    repair = (
        stack_step()
        if package == "patchright"
        else f"tell the user; operator step: {PACKAGE_HINT}"
    )
    return f"{package}'s Chromium (revision {revision}) is not installed at {build}; {repair}"


class ChromiumNotReady(RuntimeError):
    """The Chromium fallback's browser is not installed; the message is the repair."""
