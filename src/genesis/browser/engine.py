"""Read-only status of the installed Camoufox browser engine.

Camoufox 0.5's own path lookup is NOT safe to call just to ask "is the engine
there?": ``camoufox.pkgman.camoufox_path()`` deletes an install directory that
still uses the 0.4 layout, and with ``download_if_missing=True`` (the default
on every launch) it downloads a build from inside the caller. Run from a
browser tool call, that is a delete-then-download inside an MCP request, raced
by every session's MCP process.

This module answers the question by reading files only. It never imports
camoufox, never writes, never touches the network. It is the one check used by
the ``browser_automation`` capability probe and by the Camoufox launch guard,
so the two cannot disagree.

Layouts it understands:
  * camoufox < 0.5 (no ``browser-pin.json`` in the package): one engine at the
    install root, ``<install>/version.json``.
  * camoufox >= 0.5: ``<install>/.0.5_FLAG`` marks the side-by-side layout and
    engines live at ``<install>/browsers/<repo>/<version>-<build>[-sha8]/``.
    The package's ``browser-pin.json`` names the build it was released with.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path

READY = "ready"
NO_PACKAGE = "no_package"
LEGACY_LAYOUT = "legacy_layout"
NOT_INSTALLED = "not_installed"
PIN_NOT_INSTALLED = "pin_not_installed"

_PROVISION_HINT = "run scripts/install_browser_stack.sh (bootstrap runs it too)"


@dataclass(frozen=True)
class EngineStatus:
    """Outcome of :func:`camoufox_engine_status`."""

    state: str
    detail: str
    path: Path | None = None

    @property
    def ready(self) -> bool:
        return self.state == READY


def camoufox_install_dir() -> Path:
    """Where camoufox keeps its engines: ``platformdirs.user_cache_dir("camoufox")``.

    Mirrors platformdirs on Linux without importing it: ``$XDG_CACHE_HOME`` when
    it is set to an absolute path, else ``~/.cache``.
    """
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".cache"
    return base / "camoufox"


def _package_dir() -> Path | None:
    """The installed camoufox package directory, located without importing it."""
    try:
        spec = importlib.util.find_spec("camoufox")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def camoufox_pin(package_dir: Path | None = None) -> tuple[str, str] | None:
    """``(version, build)`` the installed package pairs with, or None.

    None means a pre-0.5 package (no pin file) or an unpinned development copy
    (``{}``); callers distinguish the two by whether the file exists.
    """
    pkg = package_dir if package_dir is not None else _package_dir()
    if pkg is None:
        return None
    data = _read_json(pkg / "browser-pin.json")
    if not data or not data.get("version") or not data.get("build"):
        return None
    return str(data["version"]), str(data["build"])


def _engine_ids(version_json: Path) -> tuple[str, str] | None:
    """``(version, build)`` recorded in an engine's version.json.

    Engines record the build as ``release``; accept ``build`` too.
    """
    data = _read_json(version_json)
    if not data:
        return None
    version = data.get("version")
    build = data.get("release") or data.get("build")
    if not version or not build:
        return None
    return str(version), str(build)


# camoufox's own table (camoufox/__version__.py PLAYWRIGHT_BROWSER_FLOORS in 0.5.7):
# playwright >= 1.61 needs engine build beta.30 or later. Mirrored, not imported,
# because this module must not import camoufox.
_PLAYWRIGHT_FLOORS: tuple[tuple[tuple[int, int], int], ...] = (((1, 61), 30),)


def _beta_number(build: str) -> int | None:
    if not build.startswith("beta."):
        return None
    try:
        return int(build.split(".", 1)[1])
    except ValueError:
        return None


def _playwright_build_floor() -> int | None:
    """Minimum engine beta number the installed playwright needs, or None."""
    try:
        version = importlib.metadata.version("playwright")
        major, minor = (int(p) for p in version.split(".")[:2])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return None
    floor = None
    for (fmajor, fminor), beta in _PLAYWRIGHT_FLOORS:
        if (major, minor) >= (fmajor, fminor):
            floor = beta if floor is None else max(floor, beta)
    return floor


def _installed_engines(install_dir: Path) -> list[tuple[Path, tuple[str, str]]]:
    browsers = install_dir / "browsers"
    found: list[tuple[Path, tuple[str, str]]] = []
    if not browsers.is_dir():
        return found
    for repo_dir in sorted(browsers.iterdir()):
        if not repo_dir.is_dir() or repo_dir.name.startswith("."):
            continue
        for version_dir in sorted(repo_dir.iterdir()):
            ids = _engine_ids(version_dir / "version.json")
            if version_dir.is_dir() and ids:
                found.append((version_dir, ids))
    return found


def camoufox_engine_status(
    *,
    install_dir: Path | None = None,
    package_dir: Path | None = None,
) -> EngineStatus:
    """Whether Camoufox can launch without a download or a cleanup. Never raises."""
    try:
        return _status(install_dir, package_dir)
    except Exception as exc:  # noqa: BLE001 - a status read must not crash its caller
        return EngineStatus(NOT_INSTALLED, f"could not read the Camoufox install: {exc}")


def _status(install_dir: Path | None, package_dir: Path | None) -> EngineStatus:
    pkg = package_dir if package_dir is not None else _package_dir()
    if pkg is None:
        return EngineStatus(NO_PACKAGE, f"camoufox is not installed; {_PROVISION_HINT}")
    root = install_dir if install_dir is not None else camoufox_install_dir()
    pin_file_present = (pkg / "browser-pin.json").is_file()

    if not pin_file_present:
        # camoufox < 0.5: a single engine at the install root.
        ids = _engine_ids(root / "version.json")
        if not ids:
            return EngineStatus(NOT_INSTALLED, f"no Camoufox engine at {root}; {_PROVISION_HINT}")
        floor = _playwright_build_floor()
        if floor is not None and _beta_number(ids[1]) is not None and _beta_number(ids[1]) < floor:
            # playwright moved past what this engine speaks while camoufox and
            # its engine did not (a hand install, or a failed provisioning run
            # whose package restore also failed). Measured: launching then fails
            # with "Protocol error (Browser.setDefaultViewport)".
            return EngineStatus(
                NOT_INSTALLED,
                f"engine {ids[0]}-{ids[1]} is older than installed playwright supports "
                f"(needs beta.{floor}+); {_PROVISION_HINT}",
            )
        return EngineStatus(READY, "camoufox < 0.5 with its engine installed", root)

    has_files = root.is_dir() and any(root.iterdir())
    if has_files and not (root / ".0.5_FLAG").exists():
        if _engine_ids(root / "version.json"):
            return EngineStatus(
                LEGACY_LAYOUT,
                f"{root} holds a pre-0.5 engine that camoufox 0.5 would delete on launch; "
                f"{_PROVISION_HINT}",
            )
        # Residue of an interrupted 0.5 fetch (no engine, no flag): not ready, and
        # not worth protecting; camoufox's own fetch clears it.
        return EngineStatus(
            NOT_INSTALLED,
            f"{root} has no usable engine (an interrupted download?); {_PROVISION_HINT}",
        )

    engines = _installed_engines(root)
    pin = camoufox_pin(pkg)
    if pin is None:
        # Unpinned development copy: camoufox launches whatever is installed.
        if engines:
            return EngineStatus(READY, "unpinned camoufox with an engine installed", engines[0][0])
        return EngineStatus(NOT_INSTALLED, f"no Camoufox engine at {root}; {_PROVISION_HINT}")

    for path, ids in engines:
        if ids == pin:
            return EngineStatus(READY, f"Camoufox {pin[0]}-{pin[1]}", path)
    installed = ", ".join(f"{v}-{b}" for _, (v, b) in engines) or "none"
    return EngineStatus(
        PIN_NOT_INSTALLED,
        f"camoufox needs engine {pin[0]}-{pin[1]} (installed: {installed}); {_PROVISION_HINT}",
    )
