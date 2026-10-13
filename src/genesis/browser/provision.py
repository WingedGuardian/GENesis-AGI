"""Install what the browser tools launch: the Camoufox engine the installed camoufox
package pairs with, the Chromium the fallback launches (patchright's when it is
installed, else playwright's: ``chromium.launch_package``), and the system
libraries both need.

Run through ``scripts/install_browser_stack.sh``: by ``genesis-browser-stack.service``
(which bootstrap.sh starts and a daily timer retries), inline by install.sh, or by
an operator; never from a session: every session's MCP server shares the engine
cache and the venv. Upstream's installers do the work
(``camoufox fetch``, ``<package> install chromium``, ``playwright install-deps``);
this module decides when each runs and checks the result, so a re-run is a no-op
and an interrupted run is finished by the next one.

The outcome is one line, ``browser-stack: engine=… chromium=… libs=…``, and the
exit code is 0 only when the stack is ready, or deliberately skipped (low disk with
nothing left to install, another run, camoufox older than its pin).
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import logging
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from genesis.browser import chromium, engine, packages
from genesis.browser.types import BROWSER_PGREP_PATTERNS
from genesis.env import genesis_home
from genesis.hostmetrics.readings import disk_device, existing_parent
from genesis.util.atomic import atomic_write_text
from genesis.util.disk_headroom import effective_free_bytes

logger = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]

# No hang has been observed; this is the project's floor for an unattended step
# (genesis-development, Timeout Policy). The engine download is about 1.3 GB.
STEP_TIMEOUT_S = 7200
# The unit's TimeoutStartSec (28800, four steps at the floor) less the fixed-timeout
# probes outside this budget, at worst: the dry-run 600 s, ten ldd runs at 120 s
# (four binaries before and after install-deps, two rechecks), ten dg_measure at
# 60 s (every destination of every space check), sudo 60 s: about 2,460 s, plus room
# to print the outcome. Every step's timeout is cut to what is left of it, so steps
# that each stay under their own limit cannot together outlast the unit.
RUN_BUDGET_S = 28800 - 3000
_DEFAULT_MIN_FREE_GB = 4.0
# Both browsers in one call: Chromium's package list lacks the GTK libraries
# Firefox needs (MEASURED: libxul.so links libgtk-3).
_INSTALL_DEPS = ("-m", "playwright", "install-deps", "chromium", "firefox")


@dataclass
class Outcome:
    engine: str
    chromium: str
    libs: str
    ok: bool
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"browser-stack: engine={self.engine} chromium={self.chromium} libs={self.libs}"


# ── preconditions ─────────────────────────────────────────────────────────────


def min_free_bytes() -> int:
    """``GENESIS_BROWSER_MIN_FREE_GB`` (default 4): free space below which nothing runs."""
    try:
        gb = float(os.environ.get("GENESIS_BROWSER_MIN_FREE_GB", ""))
    except ValueError:
        gb = _DEFAULT_MIN_FREE_GB
    if not math.isfinite(gb) or gb <= 0:
        gb = _DEFAULT_MIN_FREE_GB
    # A floor no disk can meet skips every run; capped so the byte count stays finite.
    return int(min(gb, 2.0**40) * 1024**3)


def free_bytes(path: Path) -> int | None:
    """Space a write under ``path`` (which may not exist yet) can use: the disk
    guardian's effective figure, which a btrfs quota tightens. None: unmeasurable."""
    return effective_free_bytes(path)


def _short_of_space(paths: list[Path], writes: Iterable[tuple[Path, int]] = ()) -> str | None:
    """Why a destination lacks room, or None. Each of ``paths`` must keep the minimum
    free; ``writes`` ((destination, bytes) a step is about to put there; one path may
    appear twice, as when the browsers dir is the download dir) must fit on
    top of it, summed per filesystem, so a download cannot itself take the disk
    below the floor. The engine, its download and Chromium can each be elsewhere."""
    floor = min_free_bytes()
    # Writes are summed per device (st_dev) and every destination on it must hold the
    # sum by the guardian's quota-aware figure: on btrfs a subvolume is its own st_dev
    # and quota, so writes sharing one share its room; an ext4/XFS project quota on a
    # shared device can only make this refuse a run that would fit (the timer
    # retries), never let one overrun the floor.
    need: dict[object, tuple[list[Path], int]] = {}
    for path, size in [(p, 0) for p in paths] + list(writes):
        key = disk_device(path) or path
        members, total = need.get(key, ([], floor))
        need[key] = ([*members, path] if path not in members else members, total + size)
    for members, wanted in need.values():
        for path in members:
            free = free_bytes(path)
            if free is None:
                return f"cannot measure {path}"
            if free < wanted:
                return (
                    f"low disk: {path} has {free / 1024**3:.1f} GB free, "
                    f"{wanted / 1024**3:.1f} GB needed"
                )
        # And the filesystem itself: quotas can each have room the pool lacks.
        physical = _device_free(members[0])
        if physical is not None and physical < wanted:
            return (
                f"low disk: the filesystem of {members[0]} has {physical / 1024**3:.1f} GB "
                f"free, {wanted / 1024**3:.1f} GB needed"
            )
    return None


def _device_free(path: Path) -> int | None:
    """The filesystem's own free bytes (statvfs), shared by every quota on it."""
    try:
        return shutil.disk_usage(existing_parent(path)).free
    except OSError:
        return None


def _writable(path: Path) -> bool:
    """Whether a write under ``path`` can succeed here: its nearest existing parent
    is writable and not read-only mounted (the unit's ProtectSystem=strict makes
    everything outside ReadWritePaths read-only, which access(2) reports)."""
    return os.access(existing_parent(path), os.W_OK)


def _unwritable(paths: list[Path]) -> str | None:
    for path in paths:
        if not _writable(path):
            return (
                f"not writable: {path} (check its permissions; genesis-browser-stack.service "
                "writes only under $HOME, so a destination elsewhere needs a ReadWritePaths= "
                "drop-in)"
            )
    return None


# What a step writes (MEASURED on 156.0.1-beta.36 and Chromium 1234): the engine
# unpacks to about 1.3 GB from an archive of about as much; the two Chromium builds
# unpack to 0.65 GB from about 0.3 GB of archives.
_ENGINE_BYTES = 1300 * 1024**2
_ENGINE_ARCHIVE_BYTES = 1300 * 1024**2
_CHROMIUM_BYTES = 700 * 1024**2
_CHROMIUM_ARCHIVE_BYTES = 350 * 1024**2


def _temp_root() -> Path:
    """Where the downloads go (``GENESIS_BIG_TMP`` or ~/tmp, as ``big_tmp_dir``), without
    creating it: a destination that cannot be made is reported, not raised."""
    return Path(os.environ.get("GENESIS_BIG_TMP") or Path.home() / "tmp") / "browser-stack"


def _chromium_writes(root: Path) -> list[tuple[Path, int]]:
    return [(root, _CHROMIUM_BYTES), (_temp_dir(), _CHROMIUM_ARCHIVE_BYTES)]


def _room_to_fetch() -> str | None:
    return _short_of_space(
        [],
        [
            (engine.camoufox_install_dir(), _ENGINE_BYTES),
            (_temp_root(), _ENGINE_ARCHIVE_BYTES),
        ],
    )


_deadline: float | None = None


def _timeout() -> float:
    """A step's timeout: its own limit, cut to what is left of the run's budget."""
    if _deadline is None:
        return STEP_TIMEOUT_S
    return max(1.0, min(STEP_TIMEOUT_S, _deadline - time.monotonic()))


def mark_legacy(root: Path) -> bool:
    """Mark a non-empty cache so camoufox 0.5's fetch keeps what is in it.

    Without ``.0.5_FLAG`` that fetch deletes the whole cache first (camoufox 0.5.8
    ``__main__.py:278``): a pre-0.5 root engine, or 0.5 engines whose installer
    was killed before its last step (the flag is touched last). Never deleted
    here; returns whether it was created.
    """
    flag = root / engine.COMPAT_FLAG
    browsers = root / "browsers"
    has_engine = (root / "version.json").is_file() or (
        browsers.is_dir() and any(browsers.iterdir())
    )
    if not has_engine or flag.exists():
        return False
    flag.touch()
    return True


# ── seams over camoufox and the process table ─────────────────────────────────


def _pin_capable() -> bool:
    """Whether the installed camoufox fetches by its browser pin (>= 0.5.7). An
    older one is never fetched: 0.4's fetch empties the whole cache before
    installing (pkgman ``cleanup()``), and 0.5.3-0.5.6 have no pin."""
    version = engine._dist_version("camoufox")
    return version is not None and not engine.predates_pin(version)


def _explicit_choice() -> bool | None:
    """Whether the user picked a build with ``camoufox set``; None when unreadable."""
    try:
        from camoufox import browser_pin, multiversion

        return browser_pin.is_explicit_choice(multiversion.load_config())
    except Exception:  # noqa: BLE001 - unknown, and the caller treats it as a choice
        return None


def _remove_version(path: Path) -> bool:
    from camoufox.multiversion import remove_version

    return bool(remove_version(path))


def _pids(pattern: str) -> list[int] | None:
    """This user's pids whose command line matches ``pattern``; None when unknown."""
    try:
        proc = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", pattern],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    return [int(p) for p in proc.stdout.split() if p.isdigit()]


def _live_engine_in(path: Path) -> bool:
    """Whether a running Camoufox executes from ``path``. Unknown counts as yes."""
    pids = _pids(BROWSER_PGREP_PATTERNS[0])
    if pids is None:
        return True
    for pid in pids:
        try:
            exe = Path(os.readlink(f"/proc/{pid}/exe"))
        except FileNotFoundError:
            continue  # exited
        except OSError:
            return True
        target = path.resolve()
        if exe == target or target in exe.parents:
            return True
    return False


_PROC = Path("/proc")


def _owner(entry: Path) -> int:
    return entry.stat().st_uid


def _live_under(directory: Path) -> bool:
    """Whether one of this user's processes executes from under ``directory``.

    Its /proc/<pid>/exe says which binary runs. Some processes deny that link to
    their own user (MEASURED: 12 of 108 here, systemd, gpg-agent, tmux; every
    browser process tested allowed it), so a denied link falls back to the command
    line's first word, and only a process neither reveals counts as live. Counting
    every denied link as live would make this true on every box, for good.
    """
    target = Path(directory).resolve()
    try:
        entries = [e for e in _PROC.iterdir() if e.name.isdigit()]
    except OSError:
        return True
    for entry in entries:
        try:
            if _owner(entry) != os.getuid():
                continue
            exe = os.readlink(entry / "exe")
        except PermissionError:
            try:
                exe = (entry / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            except FileNotFoundError:
                continue
            except OSError:
                return True
        except OSError:
            continue  # exited, or no executable (a kernel thread)
        # A replaced binary reads "<path> (deleted)": only its last component
        # changes, so containment in ``target`` still holds.
        path = Path(exe)
        if path == target or target in path.parents:
            return True
    return False


# ── steps ─────────────────────────────────────────────────────────────────────


# A download that outlives this is a leftover: no step runs longer than its timeout.
_STALE_TEMP_S = STEP_TIMEOUT_S + 3600


def _temp_dir() -> Path:
    """Where the installers' downloads go: their own directory under ~/tmp (never a
    session's small TMPDIR), so a run can clear what a killed run left there. A
    SIGKILL leaves the download's temp file behind (MEASURED: 303 MB)."""
    path = _temp_root()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clear_stale_temp() -> list[str]:
    """Remove what killed runs left: camoufox's temp files, and the
    ``playwright-download-*`` directories Playwright's installer makes. Returns
    what could not be removed, since that can be what keeps the disk low."""
    now = time.time()
    failed: list[str] = []
    for entry in _temp_dir().iterdir():
        try:
            if entry.is_symlink() or now - entry.stat().st_mtime <= _STALE_TEMP_S:
                continue
            if entry.is_file():
                entry.unlink()
            elif entry.is_dir() and entry.name.startswith("playwright-download-"):
                shutil.rmtree(entry)
        except OSError as exc:
            failed.append(f"could not remove stale download {entry}: {exc}")
    return failed


def _child_env(**extra: str) -> dict[str, str]:
    # The download is a temp file in TMPDIR; this process's own TMPDIR is untouched.
    return {**os.environ, "TMPDIR": str(_temp_dir()), **extra}


def _fetch(run: Runner) -> None:
    run(
        [sys.executable, "-m", "camoufox", "fetch"],
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        timeout=_timeout(),
        check=False,
    )


def _under_browsers(path: Path | None) -> bool:
    """A pinned engine's directory, ``<cache>/browsers/<repo>/<version>``."""
    if path is None:
        return False
    browsers = engine.camoufox_install_dir() / "browsers"
    directory = path.parent
    if directory.parent.parent != browsers:
        return False
    # Nothing here may be a link: a deletion or chmod through one would act outside
    # the cache. The cache root itself may be one (a cache moved to another disk).
    if browsers.is_symlink() or directory.parent.is_symlink() or directory.is_symlink():
        return False
    try:
        return directory.resolve().is_relative_to(browsers.resolve())
    except OSError:
        return False


def _resolve() -> engine.EngineStatus:
    """The resolver's answer, plus the one file it does not check: libxul.so, which
    ships inside the engine (install-deps cannot restore it)."""
    status = engine.resolve_camoufox_engine()
    if status.ready and status.path is not None and status.path.is_file():
        libxul = status.path.parent / "libxul.so"
        if not libxul.is_file():
            return engine.EngineStatus(
                engine.INVALID, f"{libxul} is missing from the engine", status.path
            )
    return status


def _engine_step(run: Runner, notes: list[str]) -> tuple[str, bool, engine.EngineStatus]:
    status = _resolve()
    if status.ready:
        _forget_failed_fetches()
        return status.detail, True, status
    if status.kind in (engine.NO_PACKAGE, engine.PLAYWRIGHT_MISSING):
        notes.append(status.detail)
        return "no_package", False, status
    if not _pin_capable():
        notes.append(f"camoufox is older than 0.5.7; the engine is not fetched: {status.detail}")
        return "skipped (camoufox <0.5.7)", True, status
    if _explicit_choice() is not False:
        notes.append(
            "a `camoufox set` choice (or an unreadable camoufox config) would make fetch install "
            "that choice instead of the paired build; operator step: "
            f"`{shlex.join([sys.executable, '-m', 'camoufox', 'set', '--release'])}`, "
            "then run this again"
        )
        return "user-selected", False, status
    # A directory the operator named is theirs to fix; it is never removed here.
    override = bool(os.environ.get("CAMOUFOX_EXECUTABLE_PATH", "").strip())
    fetched = chmodded = False
    for _ in range(4):  # chmod, remove+fetch, and their re-resolves; never spins
        if status.kind == engine.INVALID and _under_browsers(status.path) and not override:
            directory = status.path.parent
            if _live_engine_in(directory):
                notes.append(f"{directory} is invalid but a running Camoufox uses it; not replaced")
                return "invalid", False, status
            if "not executable" in status.detail:
                if chmodded:
                    notes.append(f"{directory} stays not executable after chmod (a noexec mount?)")
                    break
                # A fetch killed between writing version.json and its final chmod
                # (multiversion.install_versioned): finish that step, never re-download.
                if blocked := _unwritable([directory]):
                    notes.append(blocked)
                    return "not writable", False, status
                _chmod_tree(directory)
                mark_legacy(engine.camoufox_install_dir())  # the flag is that installer's last step
                chmodded = True
            elif fetched:
                break  # a fresh fetch is still invalid: deleting it again would not help
            elif _fetched_invalid_recently(directory):
                # A run of ours fetched it and it stayed invalid: fetching it again
                # daily (the timer) would repeat a 1.3 GB download forever.
                notes.append(
                    f"{directory} was fetched in the last {_RECENT_DAYS} days and stayed "
                    "invalid, so it is not downloaded again; remove it by hand to retry"
                )
                break
            else:
                if blocked := _unwritable([engine.camoufox_install_dir(), _temp_root()]):
                    notes.append(blocked)
                    return "not writable", False, status
                if short := _room_to_fetch():  # checked before anything is removed
                    return f"skipped ({short})", False, status
                _remove_version(directory)
                _fetch(run)
                fetched = True
        elif status.kind == engine.NOT_INSTALLED and not fetched:
            if blocked := _unwritable([engine.camoufox_install_dir(), _temp_root()]):
                notes.append(blocked)
                return "not writable", False, status
            if short := _room_to_fetch():
                return f"skipped ({short})", False, status
            _fetch(run)
            fetched = True
        else:
            break
        status = _resolve()
        if status.ready:
            _forget_failed_fetches()
            notes.append(
                "the browser_automation capability reflects this at the next genesis-server restart"
            )
            return status.detail, True, status
    if (
        status.kind == engine.INVALID
        and status.path is not None
        and "browsers" in status.path.parts
        and not _under_browsers(status.path)
    ):
        notes.append(
            f"{status.path.parent} is reached through a link or lies outside the engine "
            "cache; not changed here"
        )
    notes.append(status.detail)
    if status.kind == engine.NOT_INSTALLED:
        return "missing", False, status
    if fetched and status.kind == engine.INVALID and _under_browsers(status.path):
        _record_failed_fetch(status.path.parent)
    return ("invalid (after fetch)" if fetched else "invalid"), False, status


_RECENT_DAYS = 7


def _failed_fetches_path() -> Path:
    return genesis_home() / "state" / "browser-stack-failed-fetches.json"


def _failed_fetches() -> dict[str, float]:
    """{engine directory: when a fetch of ours left it invalid}. Unreadable reads as
    none: the worst that costs is one more fetch."""
    try:
        data = json.loads(_failed_fetches_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int | float)}


def _fetched_invalid_recently(directory: Path) -> bool:
    """Whether a fetch of ours left ``directory`` invalid within the last week. Not
    the age of its version.json: that says the install completed, and an engine
    damaged since (files removed) needs the repair."""
    when = _failed_fetches().get(str(directory))
    # A record from the future (a clock rolled back) is stale, not eternal.
    return when is not None and 0 <= time.time() - when < _RECENT_DAYS * 86400


def _record_failed_fetch(directory: Path) -> None:
    record = {**_failed_fetches(), str(directory): time.time()}
    try:
        _failed_fetches_path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_failed_fetches_path(), json.dumps(record))
    except OSError as exc:
        logger.warning("could not record the failed fetch of %s: %s", directory, exc)


def _forget_failed_fetches() -> None:
    try:
        _failed_fetches_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not clear %s: %s", _failed_fetches_path(), exc)


def _chmod_tree(directory: Path) -> None:
    for root, dirs, files in os.walk(directory):
        for name in (*dirs, *files):
            path = Path(root) / name
            if not path.is_symlink():
                path.chmod(0o755)
    directory.chmod(0o755)


_EXTRAS = (
    "from camoufox.addons import DefaultAddons, maybe_download_addons; "
    "maybe_download_addons(list(DefaultAddons)); "
    "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"
)


def _extras_step(run: Runner, notes: list[str]) -> bool:
    """camoufox's own idempotent installers for its default add-ons and fingerprint
    model, which ``fetch`` runs and a launch otherwise downloads mid-call. False when
    they failed: a launch runs the same installers first and fails the same way."""
    proc = run(
        [sys.executable, "-c", _EXTRAS],
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        timeout=_timeout(),
        check=False,
    )
    if proc.returncode != 0:
        notes.append(
            "camoufox's add-ons or fingerprint model did not install; a launch runs the "
            "same install first, so the engine is not ready until it succeeds"
        )
        return False
    return True


# Upstream's own repair of one build, under its installer lock: re-download and
# replace it. A plain install skips any build whose completion marker exists.
_FORCE = {
    "chromium": ("--force", "--no-shell", "chromium"),
    "chromium-headless-shell": ("--force", "chromium-headless-shell"),
}


def _install(run: Runner, package: str, args: tuple[str, ...], **extra: str) -> None:
    run(
        [sys.executable, "-m", package, "install", *args],
        env=_child_env(**extra),
        stdin=subprocess.DEVNULL,
        timeout=_timeout(),
        check=False,
    )


def _unreadable_by_ldd(run: Runner, binary: Path) -> bool:
    """ldd failed without naming a missing library: the file itself is damaged. No
    ldd says nothing about the file; the library step reports that."""
    try:
        proc = _ldd(run, binary)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode != 0 and "not found" not in text


def _force(
    run: Runner, package: str, build: str, directory: Path, notes: list[str], gc: dict[str, str]
) -> bool:
    if _live_under(directory):
        notes.append(f"{directory} is broken but a running browser uses it; not reinstalled")
        return False
    _install(run, package, _FORCE[build], **gc)
    return True


def _chromium_step(run: Runner, notes: list[str]) -> tuple[str, bool, list[Path]]:
    package = chromium.launch_package()
    if package is None:
        return "skipped (no package)", True, []
    try:
        where = chromium.locations(package, run=run)
    except chromium.ChromiumLayoutUnknown as exc:
        notes.append(str(exc))
        return "unknown", False, []
    revision = where["chromium"][0]
    root = where["chromium"][1].parent
    # Room for a download only when one is due: a re-run with both builds in place
    # installs nothing (the installer skips a complete build).
    due = any(chromium.problem(b, where[b][1]) for b in chromium.BUILDS)
    if due and (blocked := _unwritable([root, _temp_root()])):
        notes.append(blocked)
        return "not writable", False, []
    if due and (short := _short_of_space([], _chromium_writes(root))):
        # A download is due, so the fallback stays unusable: not a success, or the
        # timer's daily skip would read as one.
        return f"skipped ({short})", False, []
    # Every install, --force included, first deletes the revisions Playwright thinks
    # unused; one a live browser runs from must stay.
    gc = {"PLAYWRIGHT_SKIP_BROWSER_GC": "1"} if _live_under(root) else {}
    for build in chromium.BUILDS:
        if chromium.needs_force(build, where[build][1]):
            _force(run, package, build, where[build][1], notes, gc)
    if due:
        _install(run, package, ("chromium",), **gc)

    def unfinished() -> list[str]:
        return [p for b in chromium.BUILDS if (p := chromium.problem(b, where[b][1]))]

    if problems := unfinished():
        notes.extend(problems)
        return "missing", False, []
    for build in chromium.BUILDS:
        binary = chromium.executable(build, where[build][1])
        if not _unreadable_by_ldd(run, binary):
            continue
        if short := _short_of_space([], _chromium_writes(root)):
            notes.append(f"{binary} is damaged; not reinstalled ({short})")
            continue  # the library step reports the damaged binary
        _force(run, package, build, where[build][1], notes, gc)
    # A forced reinstall removes the build before it downloads, so a failed one
    # leaves nothing: judge the builds again, never the attempt.
    if problems := unfinished():
        notes.extend(problems)
        return "missing", False, []
    return revision, True, [chromium.executable(b, where[b][1]) for b in chromium.BUILDS]


def _ldd(run: Runner, binary: Path) -> subprocess.CompletedProcess:
    # A browser ships its own libraries beside the binary and puts that directory
    # on the library path at launch; only the rest are the system's.
    paths = [str(binary.parent), *filter(None, os.environ.get("LD_LIBRARY_PATH", "").split(":"))]
    env = {**os.environ, "LD_LIBRARY_PATH": ":".join(paths)}
    return run(
        ["ldd", str(binary)], env=env, capture_output=True, text=True, timeout=120, check=False
    )


def _missing_libs(run: Runner, binaries: list[Path]) -> tuple[list[str], list[Path]]:
    """The libraries ``binaries`` lack, and the binaries ldd could not read at all."""
    missing: set[str] = set()
    unreadable: list[Path] = []
    for binary in binaries:
        proc = _ldd(run, binary)
        named = False
        for line in (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines():
            if "=> not found" in line:
                missing.add(line.split("=>", 1)[0].strip())
                named = True
            elif "not found (required by" in line:  # e.g. version `GLIBC_2.38' not found
                missing.add(line.split("not found", 1)[0].split(":")[-1].strip())
                named = True
        if proc.returncode != 0 and not named:
            unreadable.append(binary)  # an exit code with nothing named: not "ok"
    return sorted(missing), unreadable


def _libs_step(run: Runner, binaries: list[Path], notes: list[str]) -> tuple[str, bool]:
    if not binaries:
        return "unchecked", True
    missing, unreadable = _missing_libs(run, binaries)
    if unreadable:
        notes.append("ldd could not read: " + ", ".join(str(b) for b in unreadable))
        return "error:ldd", False
    if not missing:
        return "ok", True
    glibc = [m for m in missing if "GLIBC_" in m]
    if glibc:
        # No package install supplies a newer C library; the host itself is too old.
        notes.append(f"this host's C library is older than the browser needs ({', '.join(glibc)})")
        return "glibc-too-old:" + ",".join(glibc), False
    deps = [sys.executable, *_INSTALL_DEPS]
    try:
        sudo_ok = (
            run(["sudo", "-n", "true"], capture_output=True, timeout=60, check=False).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        sudo_ok = False  # no sudo here: the operator runs the command
    if sudo_ok:
        run(["sudo", "-n", *deps], stdin=subprocess.DEVNULL, timeout=_timeout(), check=False)
        missing, unreadable = _missing_libs(run, binaries)
        if unreadable:
            notes.append("ldd could not read: " + ", ".join(str(b) for b in unreadable))
            return "error:ldd", False
        if not missing:
            return "ok", True
        notes.append(
            "install-deps did not supply every library; if it was interrupted, "
            "`sudo dpkg --configure -a` finishes the package install"
        )
    notes.append(f"operator step: `{shlex.join(['sudo', *deps])}`")
    return "missing:" + ",".join(missing), False


# Read-only: whether camoufox's add-ons and its pinned fingerprint model are in
# place (camoufox 0.5.8 fpgen_model.is_pinned, addons manifest.json), which a launch
# would otherwise install first. Any failure, an older camoufox included, reads as
# not in place.
_EXTRAS_PRESENT = (
    "import os, sys; "
    "from camoufox.addons import DefaultAddons, get_addon_path; "
    "from camoufox.fpgen_model import is_pinned; "
    "sys.exit(0 if is_pinned() and all(os.path.exists(os.path.join("
    "get_addon_path(a.name), 'manifest.json')) for a in DefaultAddons) else 1)"
)


def _extras_present(run: Runner) -> bool:
    try:
        proc = run(
            [sys.executable, "-c", _EXTRAS_PRESENT],
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_timeout(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _already_ready(run: Runner) -> bool:
    """Whether a run would install nothing: the engine resolves, the Chromium the
    fallback launches is complete (both builds, marker and executable, where its
    package says they live), camoufox's add-ons and fingerprint model are in place,
    and the binaries' system libraries resolve. Read-only: it fetches, installs and
    writes nothing, so a low-disk run can still tell ready from merely present."""
    status = _resolve()
    if not status.ready:
        return False
    binaries: list[Path] = []
    if status.path is not None:
        binaries += [status.path, status.path.parent / "libxul.so"]
    package = chromium.launch_package()
    if package is not None:
        try:
            where = chromium.locations(package, run=run)
        except chromium.ChromiumLayoutUnknown:
            return False
        if any(chromium.problem(b, where[b][1]) for b in chromium.BUILDS):
            return False
        for build in chromium.BUILDS:
            if (exe := chromium.executable(build, where[build][1])) is not None:
                binaries.append(exe)
    if _pin_capable() and not _extras_present(run):
        return False
    binaries = [b for b in binaries if b.is_file()]
    if binaries:
        missing, unreadable = _missing_libs(run, binaries)
        if missing or unreadable:
            return False
    return True


def provision(run: Runner | None = None) -> Outcome:
    """Bring the browser stack to ready; idempotent. ``run`` defaults to
    ``subprocess.run``, looked up at call time."""
    global _deadline
    run = run or subprocess.run
    notes: list[str] = []
    _deadline = time.monotonic() + RUN_BUDGET_S
    root = engine.camoufox_install_dir()
    temp = _temp_root()
    # Before the space check: a killed run's archive can be what takes the disk
    # below the floor, and a skip would then never reclaim it.
    try:
        notes.extend(_clear_stale_temp())
    except OSError as exc:
        notes.append(f"could not clear stale downloads in {temp}: {exc}")
    short = _short_of_space([root, temp])
    if short:
        # Success only when nothing needed downloading: after the browser packages
        # moved to a new pin, a skip leaves the browser unusable, and a unit that
        # reads as succeeded would hide that every day the timer repeats it.
        return Outcome(f"skipped ({short})", "skipped", "unchecked", _already_ready(run), notes)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        notes.append(f"cannot create {root}: {exc}")
        return Outcome("not writable", "skipped", "unchecked", False, notes)
    try:
        if mark_legacy(root):
            notes.append(f"marked the pre-0.5 engine in {root} so camoufox 0.5 keeps it")
    except OSError as exc:
        # The engine step's resolver names the repair for an unreadable cache.
        notes.append(f"could not check {root} for a pre-0.5 engine: {exc}")
    try:
        eng, eng_ok, status = _engine_step(run, notes)
    except Exception as exc:  # noqa: BLE001 - reported on the outcome line
        notes.append(f"engine step failed: {exc!r}")
        eng, eng_ok, status = f"error:{type(exc).__name__}", False, None
    if status is not None and status.ready and _pin_capable():
        try:
            extras_ok = _extras_step(run, notes)
        except Exception as exc:  # noqa: BLE001 - reported on the outcome
            notes.append(f"add-on / fingerprint model step failed: {exc!r}")
            extras_ok = False
        eng_ok = eng_ok and extras_ok
    try:
        chromium, chromium_ok, chrome_bins = _chromium_step(run, notes)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"chromium step failed: {exc!r}")
        chromium, chromium_ok, chrome_bins = f"error:{type(exc).__name__}", False, []
    binaries = list(chrome_bins)
    if status is not None and status.ready and status.path is not None:
        binaries = [status.path, status.path.parent / "libxul.so", *binaries]
    binaries = [b for b in binaries if b.is_file()]
    try:
        libs, libs_ok = _libs_step(run, binaries, notes)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"library step failed: {exc!r}")
        libs, libs_ok = f"error:{type(exc).__name__}", False
    return Outcome(eng, chromium, libs, eng_ok and chromium_ok and libs_ok, notes)


def _hold_lock() -> bool:
    """One run at a time (two fetches into one cache race). The wrapper script
    holds the lock on an inherited descriptor and names it; that is verified (same
    file, and flock succeeds on that descriptor), never trusted. A direct
    ``python -m`` run takes the lock itself."""
    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis")
    lock = home / "locks" / "browser-stack.lock"
    inherited = os.environ.get("GENESIS_BROWSER_STACK_LOCK_FD", "")
    if inherited.isdigit():
        fd = int(inherited)
        try:
            same = os.path.samestat(os.fstat(fd), os.stat(lock))
            if same:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # ours already: succeeds
                return True
        except (OSError, BlockingIOError):
            pass
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    return True  # held until the process exits


def _browser_requirements(pyproject: Path | None = None) -> list:
    """The ``browser`` extra's requirements that apply on this host (an environment
    marker that is false here drops one, as pip drops it). From ``pyproject`` when
    given: the checkout pip was just asked to install, since a pip run that fails
    leaves the previous ranges in the installed metadata. Otherwise as the installed
    Genesis records them (its Requires-Dist); a requirement belongs to the extra
    when its marker holds with ``extra == "browser"`` and not without it (a core one
    carrying, say, a python_version marker is not)."""
    from packaging.requirements import Requirement

    if pyproject is not None:
        import tomllib

        with open(pyproject, "rb") as f:
            extras = tomllib.load(f)["project"]["optional-dependencies"]
        reqs = [Requirement(line) for line in extras["browser"]]
        return [r for r in reqs if r.marker is None or r.marker.evaluate({"extra": "browser"})]
    dist = next(iter(metadata.packages_distributions().get("genesis", [])), None)
    found = []
    for line in (metadata.requires(dist) if dist else None) or []:
        req = Requirement(line)
        if (
            req.marker
            and req.marker.evaluate({"extra": "browser"})
            and not req.marker.evaluate({"extra": ""})
        ):
            found.append(req)
    return found


def unmet_packages(pyproject: Path | None = None) -> list[str]:
    """What the installed browser packages lack against the ``browser`` extra:
    each package installed, at a version its requirement admits, and camoufox
    carrying its browser pin. Read by bootstrap and install.sh after pip, whose
    exit status they cannot see."""
    versions = packages.installed_versions()
    unmet = [f"{dist} is not installed" for dist in packages.BROWSER_DISTS if not versions[dist]]
    unreadable = [d for d in packages.BROWSER_DISTS if versions[d] == packages.UNREADABLE]
    unmet += [
        f"{dist}'s package metadata is unreadable (a damaged install); reinstall it "
        f"({engine.PACKAGE_HINT})"
        for dist in unreadable
    ]
    # Ranges, the pin and the import are judged only on a readable version.
    versions = {d: (None if d in unreadable else v) for d, v in versions.items()}
    try:
        requirements = _browser_requirements(pyproject)
    except ImportError as exc:
        # Unable to check the ranges is not a range violation; presence and the pin
        # are still checked. (`packaging` comes with the Genesis venv's own deps.)
        print(f"browser-stack: version ranges not checked ({exc})", file=sys.stderr)
        requirements = []
    except Exception as exc:  # noqa: BLE001 - unreadable pyproject or metadata
        source = pyproject or "the installed Genesis metadata"
        unmet.append(
            f"the browser extra's version ranges could not be read from {source} ({exc!r})"
        )
        requirements = []
    for req in requirements:
        installed = versions.get(req.name.lower())
        if installed and not req.specifier.contains(installed, prereleases=True):
            unmet.append(f"{req.name} {installed} is outside {req.specifier}")
    if versions["camoufox"]:
        try:
            if not _pin_capable():
                unmet.append(f"camoufox {versions['camoufox']} predates the browser pin (0.5.7)")
        except engine._UnreadableMetadata as exc:  # e.g. a corrupt RECORD
            unmet.append(
                f"camoufox's package metadata is unreadable ({exc.args[-1]!r}); reinstall it "
                f"({engine.PACKAGE_HINT})"
            )
    # pip counts a package as installed while its .dist-info survives, even with
    # its module files gone; a launch then fails on the import.
    for dist, module in _LAUNCH_MODULES.items():
        if versions.get(dist) and (problem := _import_problem(module)):
            unmet.append(
                f"{dist} is installed but {module} does not import ({problem}); "
                f"reinstall it ({engine.PACKAGE_HINT})"
            )
    return unmet


# What each browser layer imports at launch.
_LAUNCH_MODULES = {
    "camoufox": "camoufox.async_api",
    "playwright": "playwright.async_api",
    "patchright": "patchright.async_api",
}


def _import_problem(module: str) -> str | None:
    """Why ``module`` does not import, or None. Run in check-packages' own process,
    never the server's: importing camoufox imports playwright."""
    try:
        importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - any failure means the launch fails too
        return f"{type(exc).__name__}: {exc}"
    return None


def _serves_checkout(
    repo: Path,
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
    module_file: Path | None = None,
) -> bool:
    """Whether this interpreter is a venv whose ``genesis`` is ``repo``'s own (an
    editable install of that checkout), so the wrapper may use it for ``repo``."""
    import genesis

    prefix = sys.prefix if prefix is None else prefix
    base_prefix = sys.base_prefix if base_prefix is None else base_prefix
    module = Path(module_file or genesis.__file__).resolve()
    return prefix != base_prefix and module.is_relative_to(Path(repo).resolve())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m genesis.browser.provision")
    parser.add_argument(
        "command",
        choices=("run", "mark-legacy", "check-packages", "venv-check"),
        nargs="?",
        default="run",
    )
    parser.add_argument(
        "--repo", type=Path, help="venv-check: the checkout this interpreter must serve"
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        help="check-packages: the checkout's pyproject.toml (default: installed metadata)",
    )
    args = parser.parse_args(argv)
    if args.command == "mark-legacy":
        mark_legacy(engine.camoufox_install_dir())
        return 0
    if args.command == "venv-check":
        return 0 if args.repo is not None and _serves_checkout(args.repo) else 1
    if args.command == "check-packages":
        unmet = unmet_packages(args.pyproject)
        for problem in unmet:
            print(f"browser-stack: {problem}")
        return 1 if unmet else 0
    if os.environ.get("CLAUDECODE"):
        print(
            "browser-stack: refusing to run inside a Claude Code session; every session's "
            "browser tools share the engine cache and the venv. Tell the user; operator "
            "step: scripts/install_browser_stack.sh",
            file=sys.stderr,
        )
        return 2
    if not _hold_lock():
        print("browser-stack: another run is in progress")
        return 0
    outcome = provision()
    for note in outcome.notes:
        print(f"  {note}")
    print(outcome.line())
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
