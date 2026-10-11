"""Which Camoufox binary a browser launch may use.

Every Camoufox launch passes this module's answer as ``executable_path``. Without
it camoufox resolves the engine itself, and from 0.5 on that resolution deletes a
pre-0.5 engine and downloads the pinned one (about 1.3 GB) inside the tool call
(camoufox 0.5.7 ``pkgman.camoufox_path``). With an explicit path camoufox 0.5.7+
never reaches that code: fonts, properties and the version all resolve beside the
binary (``utils.launch_options``). camoufox 0.4 still asks ``camoufox_path`` for
its root engine on every launch, which empties the cache and downloads again when
the root is outside the package's supported range, so the resolver applies that
same range check to the root (``pkgman.Version.is_supported``).

The selection is camoufox's own, read without side effects:
- camoufox < 0.5: the root engine in the cache, the binary launched today;
- camoufox >= 0.5.7: the installed engine the package pin names
  (``browser_pin.load_pin`` / ``matches``, ``multiversion.list_installed``);
- ``CAMOUFOX_EXECUTABLE_PATH``, upstream's documented override, wins when set on
  camoufox >= 0.5.7, the first release that has it. Before that it is ignored, as
  upstream ignores it: 0.4 reads the root engine whatever path it is given, and
  0.5.3-0.5.6 have neither the override nor a browser pin, so they are refused.

A user's ``camoufox set`` choice is deliberately not honoured, and nothing here
calls camoufox's path functions, ``camoufox set`` or ``fetch``. camoufox is only
imported inside the resolver's functions, never at module level: importing any
camoufox module imports playwright, which genesis-server never loads; it asks
through ``python -m genesis.browser.engine --json`` instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from genesis.env import repo_root

logger = logging.getLogger(__name__)

READY = "ready"
NOT_INSTALLED = "not_installed"
INVALID = "invalid"
NO_PACKAGE = "no_package"
PLAYWRIGHT_MISSING = "playwright_missing"

# Linux only: the binary name camoufox's pkgman.LAUNCH_FILE gives for "lin".
LAUNCH_FILE = "camoufox-bin"
# camoufox 0.5's marker that the cache holds no pre-0.5 data to clean up
# (multiversion.COMPAT_FLAG). Absent, 0.5's fetch deletes the whole cache.
COMPAT_FLAG = ".0.5_FLAG"
PACKAGE_HINT = "install the Genesis `browser` extra"
_TELL = "tell the user; operator step, with no browser open: "
# Files camoufox reads beside an explicit executable_path on every launch, raising
# when one is missing: properties.json (utils._load_properties) and, from 0.5.7 on
# Linux, fontconfig/<os>/fonts.conf or the older fontconfigs/<os>/
# (utils.get_env_vars). Genesis passes no os=, so camoufox may pick any of the three.
_FONTCONFIG_OSES = ("linux", "macos", "windows")
_READS_FONTCONFIG = sys.platform.startswith("linux")


@dataclass(frozen=True)
class EngineStatus:
    kind: str
    detail: str
    path: Path | None = None

    @property
    def ready(self) -> bool:
        return self.kind == READY

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "path": str(self.path) if self.path else None,
        }

    @classmethod
    def from_json(cls, data: dict) -> EngineStatus:
        path = data.get("path")
        return cls(data["kind"], data["detail"], Path(path) if path else None)


class CamoufoxNotReady(RuntimeError):
    """No launchable Camoufox engine; the message says why and what to tell the user."""


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _dist_files(name: str) -> list[str] | None:
    """The distribution's installed files, or None when the metadata does not say."""
    try:
        files = metadata.files(name)
    except metadata.PackageNotFoundError:
        return None
    return None if files is None else [str(f) for f in files]


def camoufox_install_dir() -> Path:
    """Where camoufox keeps its engines: ``platformdirs.user_cache_dir("camoufox")``,
    the resolver camoufox itself uses (``pkgman.INSTALL_DIR``). Without platformdirs,
    its Linux rule: a non-blank ``$XDG_CACHE_HOME``, else ``~/.cache``."""
    try:
        from platformdirs import user_cache_dir
    except ImportError:
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        base = Path(xdg) if xdg.strip() else Path.home() / ".cache"
        return base / "camoufox"
    return Path(user_cache_dir("camoufox"))


def _q(path: object) -> str:
    # Paths go into shell steps an operator may paste.
    return shlex.quote(str(path))


def stack_step() -> str:
    """The repair for an engine camoufox 0.5.7+ pins: the provisioning script, which
    marks a pre-0.5 root engine first, finishes a killed install's chmod, removes a
    corrupt pinned directory and fetches it once (genesis.browser.provision)."""
    return f"{_TELL}`{_q(repo_root() / 'scripts' / 'install_browser_stack.sh')}`"


def _operator_step(*, remove_first: bool = False) -> str:
    """What to tell the user about an engine camoufox < 0.5 uses, which the
    provisioning script never fetches: 0.4's fetch empties the whole cache before
    installing (pkgman ``cleanup()``). Run by an operator with no browser open, never
    from a session: every session's MCP server shares the engine cache and the venv.
    ``remove_first``: 0.4's fetch does nothing when the root already holds the latest
    release (``is_updated_needed``), and both its fetch and remove crash on an
    unreadable version.json (the updater catches only FileNotFoundError). Without
    version.json, fetch reinstalls."""
    root = camoufox_install_dir()
    flag = ""
    if (root / "browsers").exists():
        flag = (
            f"note that camoufox 0.4's fetch deletes everything in {_q(root)} first, "
            "including the newer engines under browsers/; then "
        )
    if remove_first:
        flag += f"`rm -f {_q(root / 'version.json')}`, then "
    # The engine download is a temp file in TMPDIR; a session's TMPDIR is a small
    # quota, and a TMPDIR that does not exist falls back to /tmp, often a small
    # tmpfs. The interpreter is this one, the venv camoufox is installed in.
    return (
        f"{_TELL}{flag}`mkdir -p ~/tmp && TMPDIR=~/tmp {_q(sys.executable)} -m camoufox fetch`"
    )



def _repair_step(binary: Path, problem: str, kind: str) -> str:
    """The operator step that repairs ``problem``. A non-executable engine is one a
    killed fetch left (camoufox writes version.json, then chmods the directory), and
    fetch skips a directory that has version.json, so fetching again never fixes it."""
    if kind == "override":
        return f"tell the user; operator step: fix or unset CAMOUFOX_EXECUTABLE_PATH ({binary})"
    if kind == "versioned":
        return stack_step()
    if "not executable" in problem:
        return f"{_TELL}`chmod -R 755 {_q(binary.parent)}`"
    return _operator_step(remove_first=True)  # pre-0.5: the root engine


def _properties_readable(raw: bytes) -> bool:
    """Whether camoufox can load this properties.json: it parses it with orjson (a
    camoufox dependency; stricter than json about a BOM or NaN) and indexes each
    entry's ``property`` (utils._load_properties)."""
    try:
        import orjson

        data = orjson.loads(raw)
    except ImportError:
        try:
            data = json.loads(raw)
        except ValueError:
            return False
    except ValueError:  # orjson.JSONDecodeError is a ValueError
        return False
    return isinstance(data, list) and all(isinstance(e, dict) and "property" in e for e in data)


def _engine_problem(
    binary: Path, *, need_version_json: bool = True, need_fontconfig: bool = True
) -> str | None:
    """Why ``binary`` cannot be launched, or None. Stateless file checks only."""
    if not binary.is_file():
        return f"no {binary.name} at {binary.parent}"
    if not os.access(binary, os.X_OK):
        return f"{binary} is not executable (a fetch was interrupted or is still running)"
    if need_version_json and not (binary.parent / "version.json").is_file():
        return f"no version.json beside {binary}"
    # camoufox reads the version from here; without it it falls back to its own
    # engine selection (utils.resolve_verstr), which writes its config.
    try:
        ini = (binary.parent / "application.ini").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"no application.ini beside {binary}"
    if not any(line.startswith("Version=") and line[8:].strip() for line in ini.splitlines()):
        return f"application.ini beside {binary} names no Version"
    try:
        raw = (binary.parent / "properties.json").read_bytes()
    except FileNotFoundError:
        return f"no properties.json beside {binary}"
    except OSError:
        return f"properties.json beside {binary} is unreadable"
    if not _properties_readable(raw):
        return f"properties.json beside {binary} is unreadable"
    if need_fontconfig and _READS_FONTCONFIG:
        for os_dir in _FONTCONFIG_OSES:
            confs = (binary.parent / d / os_dir / "fonts.conf" for d in ("fontconfig", "fontconfigs"))
            if not any(c.is_file() for c in confs):
                return f"no fontconfig/{os_dir}/fonts.conf beside {binary}"
    return None


def _check(binary: Path, label: str, kind: str) -> EngineStatus:
    # Upstream does not require version.json beside an explicitly given binary, and
    # camoufox 0.4 never checks the fontconfig it points at.
    problem = _engine_problem(
        binary, need_version_json=kind != "override", need_fontconfig=kind != "legacy"
    )
    if problem:
        return EngineStatus(
            INVALID, f"{label}: {problem}; {_repair_step(binary, problem, kind)}", binary
        )
    return EngineStatus(READY, label, binary)


def _broken_package(version: str, what: str, exc: Exception) -> EngineStatus:
    logger.warning("camoufox %s %s", version, what, exc_info=True)
    return EngineStatus(
        INVALID,
        f"camoufox {version} {what}: {exc!r}; tell the user; "
        f"operator step: reinstall it ({PACKAGE_HINT})",
    )


def _legacy_engine(version: str) -> EngineStatus:
    binary = camoufox_install_dir() / LAUNCH_FILE
    if not binary.exists() and not (binary.parent / "version.json").exists():
        return EngineStatus(
            NOT_INSTALLED,
            f"no Camoufox engine at {binary.parent}; {_operator_step()}",
        )
    label = f"Camoufox (camoufox {version})"
    status = _check(binary, label, "legacy")
    if not status.ready:
        return status
    try:
        from camoufox.pkgman import Version
    except Exception as exc:  # noqa: BLE001 - a broken package, not a broken cache
        return _broken_package(version, "cannot be imported", exc)
    try:
        supported = Version.from_path(binary.parent).is_supported()
    except Exception as exc:  # noqa: BLE001 - upstream reading the root; never raise
        return EngineStatus(
            INVALID,
            f"{label}: version.json beside {binary} is unreadable ({exc!r}); "
            f"{_operator_step(remove_first=True)}",
            binary,
        )
    if not supported:
        return EngineStatus(
            INVALID,
            f"{label}: the engine at {binary.parent} is outside the versions camoufox "
            f"{version} supports, and launching it would delete the cache and download "
            f"another; {_operator_step()}",
            binary,
        )
    return status


def _pinned_engine(version: str) -> EngineStatus:
    try:
        from camoufox import browser_pin, multiversion
    except Exception as exc:  # noqa: BLE001 - a broken package, not a broken cache
        return _broken_package(version, "cannot be imported", exc)
    try:
        pin = browser_pin.load_pin()
    except Exception as exc:  # noqa: BLE001 - the package's own pin file
        return _broken_package(version, "has an unreadable browser pin", exc)
    if pin is None:
        return EngineStatus(
            INVALID,
            f"camoufox {version} carries no browser pin; tell the user; "
            "operator step: install camoufox>=0.5.7,<0.6",
        )
    try:
        matching = [
            iv
            for iv in multiversion.list_installed()
            if browser_pin.matches(pin, iv.repo_name, iv.version.version or "", iv.version.build)
        ]
    except Exception as exc:  # noqa: BLE001 - upstream code reading the cache; never raise
        # One unreadable entry under browsers/ fails the whole listing (upstream
        # Version() raises IndexError on an empty build, for one).
        logger.warning("Camoufox engine lookup failed", exc_info=True)
        return EngineStatus(
            INVALID,
            f"camoufox engine lookup failed: {exc!r}; tell the user; operator step: "
            f"`{_q(sys.executable)} -m camoufox list` shows the engines under "
            f"{camoufox_install_dir() / 'browsers'}; remove the unreadable one",
        )

    label = f"Camoufox {pin.repo_name} {pin.spec}"
    problems = []
    for iv in matching:
        status = _check(Path(iv.path) / LAUNCH_FILE, label, "versioned")
        if status.ready:
            return status
        problems.append(status)
    if problems:
        return problems[0]
    return EngineStatus(
        NOT_INSTALLED,
        f"{label}, the engine camoufox {version} pairs with, is not installed; {stack_step()}",
    )


def predates_pin(version: str) -> bool:
    """Whether the installed camoufox ``version`` is older than the browser pin
    (0.5.7): its installed files carry no ``camoufox/browser_pin.py``, or, for an
    installer that wrote no file list, its release number is below 0.5.7."""
    files = _dist_files("camoufox")
    if files is not None:
        return "camoufox/browser_pin.py" not in files
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        return True
    return (int(match[1]), int(match[2]), int(match[3] or 0)) < (0, 5, 7)


def resolve_camoufox_engine() -> EngineStatus:
    """The Camoufox binary to launch, or why there is none. Never raises."""
    version = _dist_version("camoufox")
    if version is None:
        return EngineStatus(
            NO_PACKAGE, f"camoufox is not installed; tell the user; operator step: {PACKAGE_HINT}"
        )
    if _dist_version("playwright") is None:
        return EngineStatus(
            PLAYWRIGHT_MISSING,
            f"playwright is not installed; tell the user; operator step: {PACKAGE_HINT}",
        )

    match = re.match(r"(\d+)\.(\d+)", version)
    if not match:
        return EngineStatus(INVALID, f"cannot read the camoufox version {version!r}")
    if (int(match[1]), int(match[2])) < (0, 5):
        return _legacy_engine(version)
    # 0.5.7 brought both the browser pin and CAMOUFOX_EXECUTABLE_PATH; 0.5.3-0.5.6
    # have neither, so they are refused before the override is read.
    if predates_pin(version):
        return EngineStatus(
            INVALID,
            f"camoufox {version} predates the browser pin (0.5.7); "
            f"tell the user; operator step: install camoufox>=0.5.7,<0.6 or <0.5",
        )
    override = os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "").strip()
    if override:
        return _check(Path(os.path.abspath(override)), "CAMOUFOX_EXECUTABLE_PATH", "override")
    return _pinned_engine(version)


def main(argv: list[str] | None = None) -> int:
    status = resolve_camoufox_engine()
    if "--json" in (argv if argv is not None else sys.argv[1:]):
        print(json.dumps(status.to_json()))
    else:
        print(f"{status.kind}: {status.detail}" + (f" ({status.path})" if status.path else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
