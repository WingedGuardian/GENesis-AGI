#!/usr/bin/env python3
"""Decide which Camoufox engines retention removes; print them, NUL-separated.

The browser stack keeps one engine per camoufox release: camoufox 0.5 installs each
version in its own ``browsers/<repo>/<version>`` directory and never deletes one,
and a cache upgraded from camoufox 0.4 still holds the 0.4 engine in its root. This
script only decides. scripts/disk_hygiene.sh deletes what it prints, through its
mount-aware deleter, so a decision bug cannot reach past the names printed here.

Nothing is printed while any of these holds:
- ``CAMOUFOX_EXECUTABLE_PATH`` names an engine (the operator's choice, in this
  environment or the runtime's secrets file), or the user
  picked a build with ``camoufox set`` (or that config is unreadable);
- the engine the installed camoufox pairs with does not resolve ready from
  ``browsers/``;
- that pairing is under 14 days old: the later of the pinned engine's install and
  the camoufox package's install. Until then the older builds are the way back;
- the running browsers cannot be read.

It also keeps everything while the cache's filesystem keeps no access times, or
the browser profile's record of its last engine cannot be read.

Otherwise it prints, never an engine a running browser uses, the one the browser
profile last ran (its compatibility.ini, which Firefox writes on every launch, so an
override this process cannot see is covered), or one launched in the last 14 days
(its binary's access time):
- what an interrupted run left in ``browsers/.retiring/`` (the shell renames a
  version directory there before deleting it, so a run killed midway is finished
  by the next one; camoufox's listing skips dot directories);
- every other version directory camoufox lists;
- the pre-0.5 root engine, while its ``version.json`` or ``precomplete`` remains:
  ``version.json`` first (that alone retires it for camoufox), then the files its
  own Firefox ``precomplete`` manifest names plus camoufox's additions measured on a
  live 0.4 install, with the manifest last, so a run killed midway still finds it.
  The names camoufox 0.5 keeps in the root are never printed; a root entry neither
  list names stays and is reported.

usage: prune_browser_engines.py <camoufox cache root, canonical>
"""

from __future__ import annotations

import configparser
import os
import re
import sys
import time
from importlib import metadata
from pathlib import Path

from genesis.browser import engine, provision
from genesis.browser.types import CAMOUFOX_PROFILE_DIR

GRACE_DAYS = 14
RETIRING = ".retiring"
# camoufox 0.5.8 keeps these in the root: every `INSTALL_DIR / …` it names.
KEEP = frozenset(
    {"browsers", "config.json", "repo_cache.json", ".0.5_FLAG", "addons", "geoip", "fontconfig"}
)
# The top-level names of a live 0.4 engine's precomplete manifest (135.0.1-beta.24),
# used when the manifest is gone.
_MANIFEST_NAMES = frozenset(
    [
        "application.ini",
        "browser",
        "camoufox",
        "camoufox-bin",
        "camoufox.cfg",
        "defaults",
        "dependentlibs.list",
        "fonts",
        "glxtest",
        "gmp-clearkey",
        "libfreeblpriv3.so",
        "libgkcodecs.so",
        "libipcclientcerts.so",
        "liblgpllibs.so",
        "libmozavcodec.so",
        "libmozavutil.so",
        "libmozgtk.so",
        "libmozsandbox.so",
        "libmozsqlite3.so",
        "libmozwayland.so",
        "libnspr4.so",
        "libnss3.so",
        "libnssckbi.so",
        "libnssutil3.so",
        "libplc4.so",
        "libplds4.so",
        "libsmime3.so",
        "libsoftokn3.so",
        "libssl3.so",
        "libxul.so",
        "omni.ja",
        "pingsender",
        "platform.ini",
        "precomplete",
        "removed-files",
        "vaapitest",
    ]
)
# camoufox's additions to Firefox's files, absent from that manifest (MEASURED live).
_CAMOUFOX_NAMES = frozenset(
    {"camoucfg.jvv", "chrome.css", "distribution", "fontconfigs", "properties.json", "version.json"}
)
_MANIFEST_LINE = re.compile(r'\s*(?:remove|rmdir)\s+"([^"]+)"')
_DAY = 86400


class _Unknown:
    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = _Unknown()  # an input that exists but cannot be read: keep every engine


def _legacy_names(root: Path) -> set[str]:
    names = set(_MANIFEST_NAMES | _CAMOUFOX_NAMES)
    try:
        for line in (root / "precomplete").read_text(errors="replace").splitlines():
            match = _MANIFEST_LINE.match(line)
            if match:
                names.add(match[1].split("/", 1)[0])
    except OSError:
        pass
    return names - KEEP - {"", ".", ".."}


def _under(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _launched_recently(binary: Path, now: float) -> bool:
    """Whether the engine binary was read (launched) within the grace period: its
    access time, kept by relatime mounts; under noatime this only ever says no."""
    try:
        return now - binary.stat().st_atime < GRACE_DAYS * _DAY
    except OSError:
        return False


def plan(
    root: Path,
    status: engine.EngineStatus,
    installed: list[Path],
    exes: list[Path] | None,
    now: float,
    *,
    override: bool = False,
    explicit_choice: bool | None = False,
    paired_at: float | None = None,
    last_used: Path | _Unknown | None = None,
    atime_kept: bool = True,
) -> tuple[list[Path], list[str]]:
    """The paths to delete, in order, and why anything was kept."""
    if override:
        return [], ["kept every engine: CAMOUFOX_EXECUTABLE_PATH names the engine in use"]
    if explicit_choice is not False:
        return [], ["kept every engine: a `camoufox set` choice (or an unreadable config)"]
    # Canonical paths throughout: the root and /proc/<pid>/exe are, camoufox's
    # listing is not when the cache sits behind a symlink. A version directory that
    # is itself a symlink resolves outside browsers/ and is never printed.
    browsers = root / "browsers"
    pinned = status.path.parent.resolve() if status.path is not None else None
    if not status.ready or pinned is None or pinned.parent.parent != browsers:
        return [], [f"kept every engine: the paired engine is not the one in use ({status.detail})"]
    installed_at = _mtime(pinned / "version.json")
    known = [t for t in (installed_at, paired_at) if t is not None]
    since = max(known) if installed_at is not None else None
    if since is None or now - since < GRACE_DAYS * _DAY:
        return [], [f"kept every engine: the pairing with {pinned} is under {GRACE_DAYS} days old"]
    if exes is None:
        return [], ["kept every engine: the running browsers could not be read"]
    if not atime_kept:
        return [], [f"kept every engine: {root} keeps no access times, so no launch shows"]
    if isinstance(last_used, _Unknown):
        return [], ["kept every engine: the browser profile's record of its engine is unreadable"]

    doomed: list[Path] = []
    notes: list[str] = []
    # The engine directory the browser profile last ran (Firefox writes it on every
    # launch, whatever chose the engine): an override this process cannot see too.
    last = Path(last_used).resolve() if last_used is not None else None
    if last is not None and not _under(last, root):
        notes.append(f"the browser profile last ran an engine outside {root}: {last}")

    def last_ran(directory: Path) -> bool:
        if last is None:
            return False
        if directory.parent.name == RETIRING:  # <repo>.<version>.<pid>
            return last.parent.parent == browsers and directory.name.startswith(
                f"{last.parent.name}.{last.name}."
            )
        return _under(last, directory)

    def in_use(directory: Path, binary: Path) -> bool:
        if last_ran(directory):
            notes.append(f"kept {directory}: the browser profile last ran it")
            return True
        if any(_under(exe, directory) for exe in exes):
            notes.append(f"kept {directory}: a running browser uses it")
            return True
        if _launched_recently(binary, now):
            notes.append(f"kept {directory}: launched in the last {GRACE_DAYS} days")
            return True
        return False

    try:
        leftovers = sorted((browsers / RETIRING).iterdir())
    except FileNotFoundError:
        leftovers = []
    except OSError as exc:
        leftovers = []
        notes.append(f"could not read {browsers / RETIRING}: {exc!r}")
    # A leftover gets the same checks as a version: a browser that started just
    # before its version was set aside runs from here.
    doomed.extend(d for d in leftovers if not in_use(d, d / engine.LAUNCH_FILE))

    for d in (p.resolve() for p in installed):
        if d == pinned or d.parent.parent != browsers or d.parent.name.startswith("."):
            continue
        if not in_use(d, d / engine.LAUNCH_FILE):
            doomed.append(d)

    try:
        entries = sorted(root.iterdir())
    except OSError as exc:
        entries = []
        notes.append(f"could not read {root}: {exc!r}")
    if any(e.name in ("version.json", "precomplete") for e in entries):
        names = _legacy_names(root)
        legacy = [e for e in entries if e.name in names]
        root_exes = [exe for exe in exes if _under(exe, root) and not _under(exe, browsers)]
        if root_exes:
            notes.append(f"kept the pre-0.5 engine in {root}: a running browser uses it")
        elif last == root:
            notes.append(f"kept the pre-0.5 engine in {root}: the browser profile last ran it")
        elif _launched_recently(root / engine.LAUNCH_FILE, now):
            notes.append(
                f"kept the pre-0.5 engine in {root}: launched in the last {GRACE_DAYS} days"
            )
        else:
            # version.json first (alone it retires the root for camoufox), and the
            # marker the next run finds the root by LAST, so it survives any entry
            # that stays: the manifest when there is one, else version.json itself.
            marker = (
                "precomplete" if any(e.name == "precomplete" for e in entries) else "version.json"
            )
            legacy.sort(key=lambda e: (e.name == marker, e.name != "version.json", e.name))
            doomed.extend(legacy)
            unknown = [e.name for e in entries if e.name not in names and e.name not in KEEP]
            if unknown:
                notes.append(f"left in {root}, not a known engine file: {', '.join(unknown)}")
    return doomed, notes


def _paired_at() -> float | None:
    """When the installed camoufox package was installed (its RECORD's mtime): a
    pin engine fetched long before the package that pairs with it is not old."""
    try:
        dist = metadata.distribution("camoufox")
        record = next(f for f in dist.files or [] if f.name == "RECORD")
        return _mtime(Path(dist.locate_file(record)))
    except Exception:  # noqa: BLE001 - unknown leaves the engine's own install time
        return None


def _last_used(profile: Path) -> Path | _Unknown | None:
    """The engine directory that last opened ``profile`` (Firefox's own
    compatibility.ini ``LastPlatformDir``); None when it was never opened, UNKNOWN
    when the record is there but cannot be read."""
    path = profile / "compatibility.ini"
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if not (path.exists() or path.is_symlink()):
            return None
        if not parser.read(path, encoding="utf-8"):
            return UNKNOWN
        value = parser.get("Compatibility", "LastPlatformDir").strip()
    except (configparser.Error, UnicodeDecodeError, OSError):
        return UNKNOWN
    return Path(value) if value else UNKNOWN


def _override_set() -> bool:
    """Whether an engine override is in effect: in this environment, or in the
    secrets file the runtime loads, read with the runtime's own parser. A secrets
    file that cannot be read counts as one."""
    if os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "").strip():
        return True
    try:
        from dotenv import dotenv_values

        from genesis.env import secrets_path

        path = secrets_path()
        if not path.exists():
            return False
        value = dotenv_values(path).get("CAMOUFOX_EXECUTABLE_PATH") or ""
    except Exception:  # noqa: BLE001 - unknown counts as an override
        return True
    return bool(value.strip())


def _atime_kept(root: Path) -> bool:
    """Whether ``root``'s filesystem records access times (the recent-launch
    signal); a noatime mount, or one that cannot be asked, does not."""
    try:
        return not os.statvfs(root).f_flag & os.ST_NOATIME
    except OSError:
        return False


def _inputs(root: Path) -> tuple[engine.EngineStatus, list[Path], list[Path] | None, dict]:
    status = engine.resolve_camoufox_engine()
    try:
        from camoufox import multiversion

        installed = [Path(iv.path) for iv in multiversion.list_installed()]
    except Exception as exc:  # noqa: BLE001 - no versions are retired this run
        print(f"browser engine prune: camoufox's listing failed: {exc!r}", file=sys.stderr)
        installed = []
    gates = {
        "override": _override_set(),
        "explicit_choice": provision._explicit_choice(),
        "paired_at": _paired_at(),
        "last_used": _last_used(CAMOUFOX_PROFILE_DIR),
        "atime_kept": _atime_kept(root),
    }
    return status, installed, provision.live_camoufox_exes(), gates


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.rsplit("usage: ", 1)[1].strip(), file=sys.stderr)
        return 2
    root = Path(argv[0])
    if engine.camoufox_install_dir().resolve() != root:
        print(
            f"browser engine prune: {root} is not camoufox's cache; nothing done", file=sys.stderr
        )
        return 0
    status, installed, exes, gates = _inputs(root)
    doomed, notes = plan(root, status, installed, exes, time.time(), **gates)
    for note in notes:
        print(f"browser engine prune: {note}", file=sys.stderr)
    for path in doomed:
        sys.stdout.buffer.write(os.fsencode(path) + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
