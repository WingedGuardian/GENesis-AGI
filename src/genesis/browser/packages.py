"""Refuse a browser launch on modules that no longer match the files on disk.

A pip run that changes camoufox, playwright or patchright under a live MCP
process leaves the OLD modules this process already imported in sys.modules
(playwright arrives with the first web fetch or search, camoufox with the first
launch). The new files then fail in ways reinstalling cannot fix, such as an
ImportError or old camoufox looking for its engine where the new layout moved it.
Only a restart loads the new versions.

The baseline is taken when this module is imported, which ``mcp/health/browser.py``
does at module level, so it comes before anything imports playwright.
"""

from __future__ import annotations

import sys
from importlib import metadata

from genesis.browser.engine import PACKAGE_HINT

BROWSER_DISTS = ("camoufox", "playwright", "patchright")
# What each layer imports, so one layer's stale package never refuses another:
# camoufox drives Firefox through playwright and never loads patchright; the
# Chromium fallback, remote CDP and TinyFish import playwright only.
CAMOUFOX_DISTS = ("camoufox", "playwright")
PLAYWRIGHT_ONLY = ("playwright",)


# A distribution that is installed but whose .dist-info cannot be parsed: bad UTF-8
# in METADATA raises UnicodeDecodeError, not PackageNotFoundError.
UNREADABLE = "unreadable metadata"


def installed_versions() -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for dist in BROWSER_DISTS:
        try:
            found[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            found[dist] = None
        except Exception:  # noqa: BLE001 - a damaged install, reported as one
            found[dist] = UNREADABLE
    return found


def _is_loaded(dist: str) -> bool:
    # A None entry blocks an import; it is not a loaded module.
    return sys.modules.get(dist) is not None


# What was on disk when this process started.
STARTUP_VERSIONS = installed_versions()

# Stands for "loaded, but which version is unknown": never equal to a version.
_UNKNOWN = object()

# Per distribution, the on-disk version of the module this process LOADED. Seeded
# with what was already imported at startup; a distribution imported later is
# recorded when a check first sees it in sys.modules, so a package installed after
# startup and imported fresh is not mistaken for a stale one.
_loaded: dict[str, object] = {d: v for d, v in STARTUP_VERSIONS.items() if _is_loaded(d)}
# The on-disk versions at the most recent check (startup, before the first).
_last_seen: dict[str, str | None] = dict(STARTUP_VERSIONS)


class BrowserPackagesChanged(RuntimeError):
    """Browser packages changed on disk after this process imported them."""


def packages_changed_message(stale: list[str]) -> str | None:
    """Why a restart is needed, or None. ``stale`` names loaded distributions whose
    files changed (stale_loaded_modules); a change to one nothing loaded needs no
    restart, so there is no "any change since startup" form."""
    if not stale:
        return None
    now = installed_versions()

    def was(d: str) -> str:
        v = _loaded.get(d, STARTUP_VERSIONS[d])
        return "an unknown version" if v is _UNKNOWN else str(v)

    changed = ", ".join(f"{d} {was(d)} -> {now[d]}" for d in stale)
    if any(now[d] is None or now[d] == UNREADABLE for d in stale):
        # Uninstalled or damaged under the session: a restart alone will not repair it.
        advice = (
            "Restart this Claude Code session; if the browser is still unavailable "
            f"after that, tell the user; operator step: {PACKAGE_HINT}."
        )
    else:
        advice = "Restart this Claude Code session to load them; do not reinstall."
    return f"Browser packages changed after this session started ({changed}). {advice}"


def stale_loaded_modules(dists: tuple[str, ...] = BROWSER_DISTS) -> list[str]:
    """The distributions among ``dists`` whose loaded module no longer matches disk.

    Only a distribution this process has imported matters: one not yet loaded will
    be imported fresh from the files on disk, so a restart would load the same
    files. A distribution first seen loaded since the previous check was imported
    in between; when the disk did not change across that interval, what it loaded
    is the current version. When it did, which one it loaded is unknown, and that
    counts as stale.
    """
    now = installed_versions()
    for d in BROWSER_DISTS:
        if _is_loaded(d) and d not in _loaded:
            _loaded[d] = now[d] if now[d] == _last_seen[d] else _UNKNOWN
    _last_seen.update(now)
    return [d for d in dists if _is_loaded(d) and _loaded[d] != now[d]]


def check_loaded_browser_modules(dists: tuple[str, ...] = BROWSER_DISTS) -> None:
    """Raise BrowserPackagesChanged when a loaded module among ``dists`` is stale."""
    stale = stale_loaded_modules(dists)
    if stale:
        raise BrowserPackagesChanged(packages_changed_message(stale))


def import_error_message(exc: ImportError, dists: tuple[str, ...] = BROWSER_DISTS) -> str:
    """``dists`` are the packages the failing layer imports. Restart advice only
    when one of them is loaded and stale: a change to a package nothing imported,
    or that this layer does not use, cannot be why its import failed."""
    changed = packages_changed_message(stale_loaded_modules(dists))
    if changed:
        return f"{changed} (import failed: {exc})"
    return f"Browser not available: {exc}; tell the user; operator step: {PACKAGE_HINT}."
