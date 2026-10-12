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

import argparse
import json
import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

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


class _UnreadableMetadata(Exception):
    """A distribution is installed but its .dist-info cannot be parsed: a METADATA
    or RECORD with bad UTF-8 raises UnicodeDecodeError, not PackageNotFoundError
    (MEASURED, Python 3.12). A broken package, reported as one. A missing or
    permission-denied METADATA is suppressed by importlib and reads as not
    installed, whose operator step is the same reinstall."""


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - any other failure is a damaged install
        raise _UnreadableMetadata(name, exc) from exc


def _dist_files(name: str) -> list[str] | None:
    """The distribution's installed files, or None when the metadata does not say."""
    try:
        files = metadata.files(name)
    except metadata.PackageNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - any other failure is a damaged install
        raise _UnreadableMetadata(name, exc) from exc
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


def _operator_step(*, legacy: bool = False, remove_first: bool = False) -> str:
    """What to tell the user. Run by an operator with no browser open, never from a
    session: every session's MCP server shares the engine cache and the venv.
    ``legacy``: the installed camoufox is < 0.5, whose fetch empties the whole cache
    before installing (pkgman ``cleanup()``). ``remove_first``: 0.4's fetch does
    nothing when the root already holds the latest release (``is_updated_needed``),
    and both its fetch and remove crash on an unreadable version.json (the updater
    catches only FileNotFoundError). Without version.json, fetch reinstalls."""
    root = camoufox_install_dir()
    flag = ""
    if legacy:
        if (root / "browsers").exists():
            flag = (
                f"note that camoufox 0.4's fetch deletes everything in {_q(root)} first, "
                "including the newer engines under browsers/; then "
            )
        if remove_first:
            flag += f"`rm -f {_q(root / 'version.json')}`, then "
    elif (root / "version.json").exists() and not (root / COMPAT_FLAG).exists():
        flag = (
            f"`touch {_q(root / COMPAT_FLAG)}` (without it camoufox 0.5's fetch deletes the "
            "pre-0.5 engine there), then "
        )
    # The engine download is a temp file in TMPDIR; a session's TMPDIR is a small
    # quota, and a TMPDIR that does not exist falls back to /tmp, often a small
    # tmpfs. The interpreter is this one, the venv camoufox is installed in.
    return f"{_TELL}{flag}`mkdir -p ~/tmp && TMPDIR=~/tmp {_q(sys.executable)} -m camoufox fetch`"


def _repair_step(binary: Path, problem: str, kind: str) -> str:
    """The operator step that repairs ``problem``. A non-executable engine is one a
    killed fetch left (camoufox writes version.json, then chmods the directory), and
    fetch skips a directory that has version.json, so fetching again never fixes it."""
    if kind == "override":
        return f"tell the user; operator step: fix or unset CAMOUFOX_EXECUTABLE_PATH ({binary})"
    if "not executable" in problem:
        return f"{_TELL}`chmod -R 755 {_q(binary.parent)}`"
    if kind == "versioned":
        return f"{_TELL}remove `{_q(binary.parent)}`, then {_operator_step().removeprefix(_TELL)}"
    return _operator_step(legacy=True, remove_first=True)  # pre-0.5: the root engine


def _properties_problem(binary: Path) -> str | None:
    """Load properties.json with camoufox's own loader (``utils._load_properties``,
    the same name and signature from 0.4 to 0.5.8), so anything it fails on, a
    launch fails on. Each entry's ``type`` is read too: 0.4 and 0.5.7 index it while
    loading, 0.5.8 when a launch sets that property (validate_config), which skips
    an entry marked "removed" first. MEASURED: every entry of a real 0.4 and 0.5.8
    engine carries both fields. When camoufox.utils cannot be imported the package
    is broken, and the package import that comes first reports it."""
    if not os.path.isfile(binary.parent / "properties.json"):
        return f"no properties.json beside {binary}"
    try:
        from camoufox.utils import _load_properties
    except Exception:  # noqa: BLE001 - a broken package is reported elsewhere
        return None
    try:
        loaded = _load_properties(path=binary)
        entries = (v for v in loaded.values() if isinstance(v, dict))
        if not all("removed" in v or "type" in v for v in entries):
            raise KeyError("type")
    except Exception as exc:  # noqa: BLE001 - what the launch would raise
        return f"properties.json beside {binary} is unreadable ({exc!r})"
    return None


def _engine_problem(
    binary: Path,
    *,
    need_version_json: bool = True,
    need_fontconfig: bool = True,
    reads_ini_version: bool = True,
) -> str | None:
    """Why ``binary`` cannot be launched, or None. Stateless file checks only."""
    if not os.path.isfile(binary):
        return f"no {binary.name} at {binary.parent}"
    if not os.access(binary, os.X_OK):
        return f"{binary} is not executable (a fetch was interrupted or is still running)"
    if need_version_json and not (binary.parent / "version.json").is_file():
        return f"no version.json beside {binary}"
    # camoufox reads the version from here; without it it falls back to its own
    # engine selection (utils.resolve_verstr), which writes its config. Decoded
    # leniently on purpose: resolve_verstr reads it with errors="replace", and
    # Firefox does not read this copy, so stray bytes do not stop a launch (MEASURED).
    try:
        ini = (binary.parent / "application.ini").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"no application.ini beside {binary}"
    # The first non-empty Version= value, as resolve_verstr takes it; its major
    # goes into the browser's fingerprint (utils.py:1082, ``split('.', 1)[0]``).
    version = next(
        (v for line in ini.splitlines() if line.startswith("Version=") and (v := line[8:].strip())),
        None,
    )
    if version is None:
        return f"application.ini beside {binary} names no Version"
    if reads_ini_version and not re.fullmatch(r"[0-9]+", version.split(".", 1)[0]):
        return f"application.ini beside {binary} names an unreadable Version {version!r}"
    problem = _properties_problem(binary)
    if problem:
        return problem
    if need_fontconfig and _READS_FONTCONFIG:
        for os_dir in _FONTCONFIG_OSES:
            problem = _fonts_conf_problem(binary, os_dir) or _fonts_groups_problem(binary, os_dir)
            if problem:
                return problem
    return None


def _fonts_conf_problem(binary: Path, os_dir: str) -> str | None:
    """Read the fonts.conf a launch would, the way it would: 0.5 takes
    fontconfig/<os>/ when it holds one, else the older fontconfigs/<os>/
    (utils.py:318-320), and reads it as text in the locale encoding
    (_generate_fontconfig, ``open(.., 'r')``). MEASURED: invalid UTF-8 there fails
    a real launch with UnicodeDecodeError. Decoded as UTF-8, the file's own encoding,
    not this process's locale: the real file has non-ASCII lines, so in an ASCII
    locale every healthy engine would read as broken (and get replaced, forever)."""
    conf = binary.parent / "fontconfig" / os_dir / "fonts.conf"
    if not os.path.exists(conf):
        conf = binary.parent / "fontconfigs" / os_dir / "fonts.conf"
    if not os.path.isfile(conf):
        return f"no fontconfig/{os_dir}/fonts.conf beside {binary}"
    try:
        with open(conf, encoding="utf-8") as f:
            f.read()
    except (OSError, UnicodeDecodeError):
        return f"{conf.parent.parent.name}/{os_dir}/fonts.conf beside {binary} is unreadable"
    return None


def _override_version_problem(binary: Path) -> str | None:
    """Upstream reads version.json beside an explicit path on every launch and
    tolerates only its absence or a FileNotFoundError, KeyError or ValueError
    (0.5.8 utils.py:212-215); anything else (an empty build's IndexError, say)
    fails the launch."""
    from camoufox.pkgman import Version  # _resolve checked it imports

    try:
        Version.from_path(binary.parent)
    except (FileNotFoundError, KeyError, ValueError):
        return None
    except Exception as exc:  # noqa: BLE001 - what the launch would raise
        return f"version.json beside {binary} is unreadable ({exc!r})"
    return None


_GROUP_KEYS = {"linux": "lin", "macos": "mac", "windows": "win"}


def _fonts_groups_problem(binary: Path, os_dir: str) -> str | None:
    """fonts/groups.json, which the same launch step reads (0.5.7 and 0.5.8
    ``_generate_fontconfig``) and tolerates only when unreadable or not JSON
    (``except (OSError, ValueError)``); valid JSON of another shape fails it."""
    fonts = os.path.join(binary.parent, "fonts")
    groups = os.path.join(fonts, "groups.json")
    if not os.path.exists(groups):
        return None
    try:
        with open(groups, "rb") as fh:
            read_by = json.loads(fh.read()).get("readBy", {}).get(_GROUP_KEYS[os_dir], [])
        for group in read_by:
            os.path.isdir(os.path.join(fonts, group))
    except (OSError, ValueError):
        return None
    except Exception as exc:  # noqa: BLE001 - what the launch would raise
        return f"fonts/groups.json beside {binary} is unreadable ({exc!r})"
    return None


def _check(binary: Path, label: str, kind: str) -> EngineStatus:
    # Upstream does not require version.json beside an explicitly given binary, and
    # camoufox 0.4 never checks the fontconfig it points at.
    problem = _engine_problem(
        binary,
        need_version_json=kind != "override",
        need_fontconfig=kind != "legacy",
        reads_ini_version=kind != "legacy",
    )
    if problem is None and kind == "override":
        problem = _override_version_problem(binary)
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
            f"no Camoufox engine at {binary.parent}; {_operator_step(legacy=True)}",
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
            f"{_operator_step(legacy=True, remove_first=True)}",
            binary,
        )
    if not supported:
        return EngineStatus(
            INVALID,
            f"{label}: the engine at {binary.parent} is outside the versions camoufox "
            f"{version} supports, and launching it would delete the cache and download "
            f"another; {_operator_step(legacy=True)}",
            binary,
        )
    return status


def _engine_dirs() -> list[Path]:
    """Every ``browsers/<repo>/<version>`` directory holding a version.json: the
    ones camoufox's ``list_installed`` reads (multiversion.py:281-291). A directory
    that cannot be read raises, as it does from the listing: the repair is its
    permissions, not an engine directory."""
    browsers = camoufox_install_dir() / "browsers"
    if not browsers.is_dir():
        return []
    found = []
    for repo in sorted(browsers.iterdir()):
        if not repo.is_dir() or repo.name.startswith("."):
            continue
        found.extend(
            d for d in sorted(repo.iterdir()) if d.is_dir() and (d / "version.json").exists()
        )
    return found


def _listing_skips() -> tuple[type[Exception], ...]:
    """The errors camoufox's ``list_installed`` skips a directory for rather than
    raising (multiversion.py:312). orjson comes with camoufox."""
    try:
        import orjson
    except ImportError:
        return (FileNotFoundError, json.JSONDecodeError)
    return (FileNotFoundError, orjson.JSONDecodeError)


def _breaks_listing(dirs: list[Path]) -> list[Path]:
    """The directories that make camoufox's ``list_installed`` raise: its own
    ``pkgman.Version.from_path`` fails on them with anything other than the
    errors the listing skips quietly."""
    from camoufox.pkgman import Version

    skipped = _listing_skips()
    bad = []
    for d in dirs:
        try:
            Version.from_path(d)
        except skipped:
            continue  # skipped by the listing, so not what broke it
        except Exception:  # noqa: BLE001 - what aborts the listing
            bad.append(d)
    return bad


def _named_for(pin, d: Path) -> bool:
    # install_versioned names a directory <version>-<build>[-<sha8>]
    # (multiversion.version_folder_name; sha8 is the asset's sha256 prefix).
    return d.parent.name.lower() == pin.repo_name and bool(
        re.fullmatch(rf"{re.escape(pin.spec)}(-[0-9a-f]{{8}})?", d.name)
    )


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
        installed = list(multiversion.list_installed())
        matching = [
            iv
            for iv in installed
            if browser_pin.matches(pin, iv.repo_name, iv.version.version or "", iv.version.build)
        ]
    except OSError:
        # list_installed walks browsers/ unguarded: a directory it cannot read is a
        # permissions repair, which resolve_camoufox_engine names.
        raise
    except Exception as exc:  # noqa: BLE001 - upstream code reading the cache; never raise
        # One unreadable entry under browsers/ fails the whole listing (upstream
        # Version() raises IndexError on an empty build, for one), and `camoufox
        # list` runs the same listing, so the step names the entries instead.
        logger.warning("Camoufox engine lookup failed", exc_info=True)
        try:
            bad = _breaks_listing(_engine_dirs())
        except Exception:  # noqa: BLE001 - nothing more to name
            bad = []
        if bad:
            named = ", ".join(f"`{_q(d)}`" for d in bad)
            step = f"the engine directories camoufox cannot read: {named}; {_TELL}remove them"
        else:
            step = (
                f"no engine directory is the cause; {_TELL}check "
                f"{_q(camoufox_install_dir() / 'config.json')}, which camoufox reads for every "
                "listing"
            )
        # A path only when exactly one directory is to blame: a caller may remove it.
        return EngineStatus(
            INVALID,
            f"camoufox engine lookup failed: {exc!r}; {step}",
            bad[0] / LAUNCH_FILE if len(bad) == 1 else None,
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
    # A directory named for the pin that does not hold it: list_installed skips one
    # whose version.json is unreadable without a word (multiversion.py:312), or its
    # version.json names another build. install_versioned refuses to replace a
    # directory with a version.json (:404), so a fetch into that name fails while
    # it stays; removing it lets the fetch through.
    listed = {Path(iv.path) for iv in installed}
    for d in _engine_dirs():
        if not _named_for(pin, d):
            continue
        if d in listed:
            problem = f"the version.json in {d} names another build than {pin.spec}"
        else:
            problem = f"the version.json in {d} is unreadable, so camoufox skips this engine"
        binary = d / LAUNCH_FILE
        return EngineStatus(
            INVALID, f"{label}: {problem}; {_repair_step(binary, problem, 'versioned')}", binary
        )
    return EngineStatus(
        NOT_INSTALLED,
        f"{label}, the engine camoufox {version} pairs with, is not installed; {_operator_step()}",
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
    try:
        return _resolve()
    except OSError as exc:
        logger.warning("could not read the Camoufox engine cache", exc_info=True)
        return EngineStatus(
            INVALID,
            f"the Camoufox engine cache could not be read ({exc!r}); tell the user; "
            f"operator step: check the permissions under {camoufox_install_dir()}",
        )
    except _UnreadableMetadata as exc:
        name, err = exc.args
        logger.warning("%s package metadata is unreadable", name, exc_info=True)
        return EngineStatus(
            INVALID,
            f"{name} is installed but its package metadata is unreadable ({err!r}); "
            f"tell the user; operator step: reinstall it ({PACKAGE_HINT})",
        )


def _resolve() -> EngineStatus:
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
        try:
            from camoufox.pkgman import Version  # noqa: F401 - the override check reads with it
        except Exception as exc:  # noqa: BLE001 - a broken package, not a broken override
            return _broken_package(version, "cannot be imported", exc)
        return _check(Path(os.path.abspath(override)), "CAMOUFOX_EXECUTABLE_PATH", "override")
    return _pinned_engine(version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m genesis.browser.engine")
    parser.add_argument("--json", action="store_true", help="print the status as JSON")
    args = parser.parse_args(argv)
    status = resolve_camoufox_engine()
    if args.json:
        print(json.dumps(status.to_json()))
    else:
        print(f"{status.kind}: {status.detail}" + (f" ({status.path})" if status.path else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
