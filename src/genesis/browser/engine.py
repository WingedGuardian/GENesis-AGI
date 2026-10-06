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

Layouts it understands, told apart as camoufox does, by the package's own code
(``multiversion.py``, which every 0.5 release ships and no 0.4 release does):
  * camoufox < 0.5: one engine at the install root, ``<install>/version.json``.
  * camoufox >= 0.5: ``<install>/.0.5_FLAG`` marks the side-by-side layout and
    engines live at ``<install>/browsers/<repo>/<version>-<build>[-sha8]/``.
    From 0.5.7 the package's ``browser-pin.json`` names the build it was
    released with; 0.5.3 to 0.5.6 have no pin and resolve as unpinned.

For 0.5 it mirrors camoufox's own resolution (multiversion.get_active_path, then
pkgman.camoufox_path's supported-range check), so READY means camoufox would
pick an installed engine and launch it without fetching: the pinned engine is
matched on (repo, version, build) as browser_pin.matches does; an unpinned copy
resolves config.json's active engine, else the newest installed; and whichever
engine that selects must lie inside the supported build range, or camoufox
fetches another. For both layouts that range is the INSTALLED package's own
(read from its __version__.py without importing it), raised by the playwright
floor.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

READY = "ready"
NO_PACKAGE = "no_package"
LEGACY_LAYOUT = "legacy_layout"
NOT_INSTALLED = "not_installed"
PIN_NOT_INSTALLED = "pin_not_installed"
OVERRIDDEN = "overridden"

# What an operator does about any not-ready state. Deliberately run by hand, with
# no browser open: the same fetch inside a tool call is what this module prevents.
PROVISION_HINT = (
    "install the `browser` extra and the Camoufox engine it pairs with "
    "(`python -m camoufox fetch`) while no browser session is running"
)

# A provisioning run holds this EXCLUSIVE for its whole transaction; each local
# browser (Camoufox, Chromium) holds it SHARED for as long as its process may be
# alive. So a launch cannot start mid-upgrade, and an upgrade cannot start under
# a browser.
BROWSER_LOCK_FILE = Path.home() / ".genesis" / "locks" / "browser-provision.lock"


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

    The same resolver camoufox uses (pkgman.INSTALL_DIR), so the two can never
    look in different places; platformdirs is a camoufox dependency. Without it,
    mirror its Linux rule: a non-blank ``$XDG_CACHE_HOME`` is used as given,
    relative or not, else ``~/.cache``.
    """
    try:
        from platformdirs import user_cache_dir
    except ImportError:
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        base = Path(xdg) if xdg.strip() else Path.home() / ".cache"
        return base / "camoufox"
    return Path(user_cache_dir("camoufox"))


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


class CamoufoxPin(NamedTuple):
    """The build a camoufox 0.5 package pairs with (its ``browser-pin.json``).

    ``version`` and ``build`` keep their positions so ``pin[0]`` is the Firefox
    version; ``repo_name`` is the cache directory (``browsers/<repo_name>/``),
    lowercased as camoufox's browser_pin.load_pin does.
    """

    version: str
    build: str
    repo_name: str


def camoufox_pin(package_dir: Path | None = None) -> CamoufoxPin | None:
    """The pin the installed package pairs with, or None.

    None means a package with no pin: any 0.4 release, 0.5.3 to 0.5.6, or a
    development copy. It says nothing about the layout; :func:`_status` reads
    that from the package code. Mirrors camoufox's
    browser_pin.load_pin: a pin without a ``tag`` is no pin (that is how the
    ``{}`` of a development checkout reads), and the repo is part of it.
    """
    pkg = package_dir if package_dir is not None else _package_dir()
    if pkg is None:
        return None
    data = _read_json(pkg / "browser-pin.json")
    if not data or not data.get("tag"):
        return None
    fields = [data.get(k) for k in ("version", "build", "repo_name")]
    if not all(isinstance(f, str) and f for f in fields):
        return None
    version, build, repo_name = fields
    return CamoufoxPin(version, build, repo_name.lower())


class _Engine(NamedTuple):
    path: Path
    repo_name: str
    version: str
    build: str
    executable: bool

    @property
    def spec(self) -> str:
        return f"{self.version}-{self.build}"

    def matches(self, pin: CamoufoxPin) -> bool:
        # camoufox browser_pin.matches: (repo_name.lower(), version, build).
        return (self.repo_name.lower(), self.version, self.build) == (
            pin.repo_name,
            pin.version,
            pin.build,
        )


def _engine_ids(version_json: Path) -> tuple[str, str] | None:
    """``(version, build)`` recorded in an engine's version.json.

    camoufox's pkgman.Version.from_path reads the build from ``release``, else
    ``tag``, else ``build``; so does this. Like it, a missing ``version`` still
    names an engine (every release reads it with ``.get``): it is ``""`` here.
    """
    data = _read_json(version_json)
    if not data:
        return None
    build = data.get("release") or data.get("tag") or data.get("build")
    if not build:
        return None
    return str(data.get("version") or ""), str(build)


# camoufox's supported engine range lives in the INSTALLED package's
# __version__.py (class CONSTRAINTS: MIN_VERSION, MAX_VERSION and, from 0.5,
# PLAYWRIGHT_BROWSER_FLOORS), and differs by release: 0.4.11 has MIN_VERSION
# 'beta.19', camoufox's main branch 'alpha.1'. _package_constraints reads it
# with ast, never importing camoufox. These are the fallbacks when it cannot:
# camoufox main's values, and its measured floor (playwright >= 1.61 needs
# engine build beta.30 or later; below it every new_context() fails with
# "Protocol error (Browser.setDefaultViewport)"), which also applies to a
# package too old to carry the table.
_MIN_BUILD = "alpha.1"
_MAX_BUILD = "1"
_PLAYWRIGHT_FLOORS: tuple[tuple[tuple[int, int], str], ...] = (((1, 61), "beta.30"),)


class _Constraints(NamedTuple):
    min_build: str
    max_build: str
    floors: tuple[tuple[tuple[int, int], str], ...]
    from_package: bool


def _valid_floors(value: object) -> bool:
    return isinstance(value, tuple) and all(
        isinstance(entry, tuple)
        and len(entry) == 2
        and isinstance(entry[0], tuple)
        and all(isinstance(n, int) for n in entry[0])
        and isinstance(entry[1], str)
        for entry in value
    )


def _package_constraints(pkg: Path) -> _Constraints:
    """camoufox's CONSTRAINTS from ``<pkg>/__version__.py``, parsed, not imported.

    Falls back to the mirrored constants when the file or a value cannot be
    read; ``from_package`` says which, so a READY verdict can say so too.
    """
    fallback = _Constraints(_MIN_BUILD, _MAX_BUILD, _PLAYWRIGHT_FLOORS, False)
    try:
        tree = ast.parse((pkg / "__version__.py").read_text())
    except (OSError, SyntaxError, ValueError):
        return fallback
    values: dict[str, object] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "CONSTRAINTS"):
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                try:
                    values[stmt.targets[0].id] = ast.literal_eval(stmt.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
    minimum, maximum = values.get("MIN_VERSION"), values.get("MAX_VERSION")
    if not (isinstance(minimum, str) and isinstance(maximum, str)):
        return fallback
    try:
        _build_key(minimum), _build_key(maximum)
    except (ValueError, IndexError):
        return fallback
    floors = values.get("PLAYWRIGHT_BROWSER_FLOORS", _PLAYWRIGHT_FLOORS)
    if not _valid_floors(floors):
        floors = _PLAYWRIGHT_FLOORS
    return _Constraints(minimum, maximum, floors, True)


def _build_key(build: str) -> tuple[int, ...]:
    """camoufox pkgman.Version.sorted_rel: numeric parts as ints, a word part as
    ``ord(first letter) - 1024``, padded to six parts. Raises on a malformed build."""
    parts = [int(x) if x.isdigit() else ord(x[0]) - 1024 for x in build.split(".")]
    return tuple(parts + [0] * (5 - build.count(".")))


def _playwright_build_floor(
    floors: tuple[tuple[tuple[int, int], str], ...] = _PLAYWRIGHT_FLOORS,
) -> str | None:
    """The minimum engine build the installed playwright needs, or None."""
    try:
        version = importlib.metadata.version("playwright")
        installed = tuple(int(p) for p in version.split(".")[:2])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return None  # camoufox's effective_version_min falls back the same way
    floor = None
    for needs_playwright, build in floors:
        if installed >= needs_playwright and (
            floor is None or _build_key(build) > _build_key(floor)
        ):
            floor = build
    return floor


def _unsupported_reason(build: str, limits: _Constraints) -> str | None:
    """Why this build cannot launch, or None: outside the package's supported
    range (pkgman.Version.is_supported, so camoufox would fetch another), or
    below the installed playwright's floor."""
    floor = _playwright_build_floor(limits.floors)
    try:
        key = _build_key(build)
        floor_key = _build_key(floor) if floor is not None else None
    except (ValueError, IndexError):
        return f"its build {build!r} is not a version camoufox can read"
    if floor_key is not None and key < floor_key and floor_key > _build_key(limits.min_build):
        return f"it is older than the installed playwright supports (needs {floor}+)"
    if key < _build_key(limits.min_build):
        return f"it is below camoufox's minimum build {limits.min_build}"
    if key >= _build_key(limits.max_build):
        return f"it is not below camoufox's maximum build {limits.max_build}"
    return None


def _engine_at(path: Path, repo_name: str) -> _Engine | None:
    ids = _engine_ids(path / "version.json")
    if not path.is_dir() or ids is None:
        return None
    return _Engine(path, repo_name, ids[0], ids[1], _has_executable(path))


def _installed_engines(install_dir: Path) -> list[_Engine]:
    """Every engine with a readable version.json, as multiversion.list_installed
    sees them: newest first within each repo, repos in reverse name order."""
    browsers = install_dir / "browsers"
    found: list[_Engine] = []
    if not browsers.is_dir():
        return found
    for repo_dir in browsers.iterdir():
        if not repo_dir.is_dir() or repo_dir.name.startswith("."):
            continue
        for version_dir in repo_dir.iterdir():
            eng = _engine_at(version_dir, repo_dir.name)
            if eng is not None:
                found.append(eng)

    def order(e: _Engine) -> tuple:
        try:
            return (e.repo_name, _build_key(e.build))
        except (ValueError, IndexError):
            return (e.repo_name, ())

    found.sort(key=order, reverse=True)
    return found


# camoufox's browser_pin.is_explicit_choice (0.5.7): a `channel` or `pinned` in
# <install>/config.json means the user chose a build other than the paired one.
# NOT `active_version`: every plain fetch records that itself (measured: a
# paired fetch wrote active_version=browsers/official/156.0.1-beta.34-09effb44).
_OVERRIDE_KEYS = ("channel", "pinned")


def _active_override(config: dict) -> str | None:
    chosen = [f"{k}={config[k]}" for k in _OVERRIDE_KEYS if config.get(k)]
    return ", ".join(chosen) or None


# camoufox's pkgman.LAUNCH_FILE: the file it executes on each platform.
_LAUNCH_FILES = {"linux": "camoufox-bin", "win32": "camoufox.exe"}


def _has_executable(engine_dir: Path) -> bool:
    """version.json alone does not make an engine: a truncated extraction can
    leave the metadata without the binary. macOS bundles are not checked."""
    name = next((f for p, f in _LAUNCH_FILES.items() if sys.platform.startswith(p)), None)
    if name is None:
        return True
    exe = engine_dir / name
    return exe.is_file() and os.access(exe, os.X_OK)


def _is_multiversion_package(pkg: Path) -> bool:
    """Whether the package uses the 0.5 side-by-side layout.

    camoufox's own switch is the code it ships: every 0.5 release has
    multiversion.py, whose COMPAT_FLAG its camoufox_path checks, and no 0.4
    release has it. browser-pin.json is NOT the switch: 0.5.3 to 0.5.6 have none.
    """
    return any((pkg / f"multiversion{ext}").is_file() for ext in (".py", ".pyc"))


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
        return EngineStatus(NO_PACKAGE, f"camoufox is not installed; {PROVISION_HINT}")
    root = install_dir if install_dir is not None else camoufox_install_dir()
    limits = _package_constraints(pkg)

    if not _is_multiversion_package(pkg):
        # camoufox < 0.5: a single engine at the install root.
        ids = _engine_ids(root / "version.json")
        if not ids or not _has_executable(root):
            return EngineStatus(NOT_INSTALLED, f"no Camoufox engine at {root}; {PROVISION_HINT}")
        # camoufox 0.4's camoufox_path fetches when the root engine is outside its
        # supported range. The playwright floor covers playwright moving past the
        # engine while camoufox did not (a hand install, or a failed provisioning
        # run whose package restore also failed).
        unsupported = _unsupported_reason(ids[1], limits)
        if unsupported:
            return EngineStatus(
                NOT_INSTALLED,
                f"engine {ids[0]}-{ids[1]} cannot launch because {unsupported}; {PROVISION_HINT}",
            )
        return EngineStatus(
            READY, f"camoufox < 0.5 with engine {ids[0]}-{ids[1]}{_unread(limits)}", root
        )

    has_files = root.is_dir() and any(root.iterdir())
    if has_files and not (root / ".0.5_FLAG").exists():
        if _engine_ids(root / "version.json"):
            return EngineStatus(
                LEGACY_LAYOUT,
                f"{root} holds a pre-0.5 engine that camoufox 0.5 would delete on launch; "
                f"{PROVISION_HINT}",
            )
        # Residue of an interrupted 0.5 fetch (no engine, no flag): not ready, and
        # not worth protecting; camoufox's own fetch clears it.
        return EngineStatus(
            NOT_INSTALLED,
            f"{root} has no usable engine (an interrupted download?); {PROVISION_HINT}",
        )

    engines = _installed_engines(root)
    pin = camoufox_pin(pkg)
    config = _read_json(root / "config.json") or {}
    override = _active_override(config)
    if pin is not None and override:
        # `camoufox set` chose another build; camoufox would launch (and fetch)
        # that one, not the paired build this status vouches for.
        return EngineStatus(
            OVERRIDDEN,
            f"a `camoufox set` choice ({override}) overrides the paired engine "
            f"{pin.version}-{pin.build}; `python -m camoufox set --release` restores "
            f"the paired build",
        )

    if pin is not None:
        # multiversion.get_active_path with a pin: the first installed engine
        # matching it, whatever config.json marks active.
        chosen = next((e for e in engines if e.matches(pin)), None)
        if chosen is None:
            installed = ", ".join(f"{e.repo_name}/{e.spec}" for e in engines) or "none"
            return EngineStatus(
                PIN_NOT_INSTALLED,
                f"camoufox needs engine {pin.repo_name}/{pin.version}-{pin.build} "
                f"(installed: {installed}); {PROVISION_HINT}",
            )
        what = f"Camoufox {pin.version}-{pin.build}"
    else:
        # Unpinned (0.5.3 to 0.5.6, or a development copy): get_active_path's
        # own order. config.json's active engine if it exists, else (unless a
        # channel or pin is chosen) the first installed one.
        chosen = None
        active = config.get("active_version")
        if isinstance(active, str) and active:
            # As camoufox resolves it (INSTALL_DIR / active), unvalidated: the
            # file is the local user's own, and this only reads what it names.
            path = root / active
            chosen = _engine_at(path, path.parent.name)
        if chosen is None and not override:
            chosen = engines[0] if engines else None
        if chosen is None:
            return EngineStatus(NOT_INSTALLED, f"no Camoufox engine at {root}; {PROVISION_HINT}")
        what = f"unpinned camoufox with engine {chosen.repo_name}/{chosen.spec}"

    if not chosen.executable:
        return EngineStatus(
            PIN_NOT_INSTALLED if pin is not None else NOT_INSTALLED,
            f"engine {chosen.repo_name}/{chosen.spec} at {chosen.path} has no executable "
            f"(a truncated extraction?); {PROVISION_HINT}",
        )
    # pkgman.camoufox_path launches the selected engine only when its build is
    # supported; otherwise it fetches another (0.5.6+) or fails (0.5.3 to 0.5.5).
    # The playwright floor is camoufox's own only from 0.5.6; below that it is
    # Genesis's, and the engine would launch and then fail to open a page.
    unsupported = _unsupported_reason(chosen.build, limits)
    if unsupported:
        return EngineStatus(
            NOT_INSTALLED,
            f"engine {chosen.repo_name}/{chosen.spec} is not usable because "
            f"{unsupported}; {PROVISION_HINT}",
        )
    return EngineStatus(READY, what + _unread(limits), chosen.path)


def _unread(limits: _Constraints) -> str:
    if limits.from_package:
        return ""
    return " (checked against Genesis's copy of camoufox's version limits: the package's own were unreadable)"
