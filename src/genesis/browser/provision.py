"""Install what the browser tools launch: the Camoufox engine the installed camoufox
package pairs with, patchright's Chromium, and the system libraries both need.

Run through ``scripts/install_browser_stack.sh``: by ``genesis-browser-stack.service``
(which bootstrap.sh starts and a daily timer retries), inline by install.sh, or by
an operator; never from a session: every session's MCP server shares the engine
cache and the venv. Upstream's installers do the work
(``camoufox fetch``, ``patchright install chromium``, ``playwright install-deps``);
this module decides when each runs and checks the result, so a re-run is a no-op
and an interrupted run is finished by the next one.

The outcome is one line, ``browser-stack: engine=… chromium=… libs=…``, and the
exit code is 0 only when the stack is ready, or deliberately skipped (low disk with
nothing left to install, another run, camoufox older than its pin).
"""

from __future__ import annotations

import argparse
import fcntl
import math
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from genesis.browser import chromium, engine, packages
from genesis.browser.types import BROWSER_PGREP_PATTERNS
from genesis.util.tmp import big_tmp_dir

Runner = Callable[..., subprocess.CompletedProcess]

# No hang has been observed; this is the project's floor for an unattended step
# (genesis-development, Timeout Policy). The engine download is about 1.3 GB.
STEP_TIMEOUT_S = 7200
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
    return int(gb * 1024**3)


def free_bytes(path: Path) -> int:
    """Free space on the filesystem that would hold ``path``, which may not exist yet."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


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


def _live_chromium() -> bool:
    """Whether a Playwright-managed Chromium runs; unknown counts as yes."""
    pids = _pids(BROWSER_PGREP_PATTERNS[1])
    return pids is None or bool(pids)


# ── steps ─────────────────────────────────────────────────────────────────────


# A download that outlives this is a leftover: no step runs longer than its timeout.
_STALE_TEMP_S = STEP_TIMEOUT_S + 3600


def _temp_dir() -> Path:
    """Where the installers' downloads go: their own directory under ~/tmp (never a
    session's small TMPDIR), so a run can clear what a killed run left there. A
    SIGKILL leaves the download's temp file behind (MEASURED: 303 MB)."""
    path = Path(big_tmp_dir()) / "browser-stack"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clear_stale_temp() -> None:
    """Remove what killed runs left: camoufox's temp files, and the
    ``playwright-download-*`` directories Playwright's installer makes."""
    now = time.time()
    for entry in _temp_dir().iterdir():
        try:
            if entry.is_symlink() or now - entry.stat().st_mtime <= _STALE_TEMP_S:
                continue
            if entry.is_file():
                entry.unlink()
            elif entry.is_dir() and entry.name.startswith("playwright-download-"):
                shutil.rmtree(entry)
        except OSError:
            continue


def _child_env(**extra: str) -> dict[str, str]:
    # The download is a temp file in TMPDIR; this process's own TMPDIR is untouched.
    return {**os.environ, "TMPDIR": str(_temp_dir()), **extra}


def _fetch(run: Runner) -> None:
    run(
        [sys.executable, "-m", "camoufox", "fetch"],
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        timeout=STEP_TIMEOUT_S,
        check=False,
    )


def _under_browsers(path: Path | None) -> bool:
    """A pinned engine's directory, ``<cache>/browsers/<repo>/<version>``."""
    if path is None:
        return False
    browsers = engine.camoufox_install_dir() / "browsers"
    return path.parent.parent.parent == browsers


def _engine_step(run: Runner, notes: list[str]) -> tuple[str, bool, engine.EngineStatus]:
    status = engine.resolve_camoufox_engine()
    if status.ready:
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
            f"`{sys.executable} -m camoufox set --release`, then run this again"
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
            if directory.is_symlink():
                notes.append(f"{directory} is a symlink; not changed here")
                break
            if "not executable" in status.detail:
                if chmodded:
                    notes.append(f"{directory} stays not executable after chmod (a noexec mount?)")
                    break
                # A fetch killed between writing version.json and its final chmod
                # (multiversion.install_versioned): finish that step, never re-download.
                _chmod_tree(directory)
                mark_legacy(engine.camoufox_install_dir())  # the flag is that installer's last step
                chmodded = True
            elif fetched:
                break  # a fresh fetch is still invalid: deleting it again would not help
            elif _installed_recently(directory):
                # An earlier run fetched it and it is still invalid: fetching it
                # again daily (the timer) would repeat a 1.3 GB download forever.
                notes.append(
                    f"{directory} was installed in the last {_RECENT_DAYS} days and is still "
                    "invalid, so it is not downloaded again; remove it by hand to retry"
                )
                break
            else:
                _remove_version(directory)
                _fetch(run)
                fetched = True
        elif status.kind == engine.NOT_INSTALLED and not fetched:
            _fetch(run)
            fetched = True
        else:
            break
        status = engine.resolve_camoufox_engine()
        if status.ready:
            notes.append(
                "the browser_automation capability reflects this at the next genesis-server restart"
            )
            return status.detail, True, status
    notes.append(status.detail)
    if status.kind == engine.NOT_INSTALLED:
        return "missing", False, status
    return ("invalid (after fetch)" if fetched else "invalid"), False, status


_RECENT_DAYS = 7


def _installed_recently(directory: Path) -> bool:
    """Whether camoufox wrote the directory's version.json (its installer's record
    of a completed extract) within the last week; unreadable reads as no."""
    try:
        age = time.time() - (directory / "version.json").stat().st_mtime
    except OSError:
        return False
    return age < _RECENT_DAYS * 86400


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


def _extras_step(run: Runner, notes: list[str]) -> None:
    """camoufox's own idempotent installers for its default add-ons and fingerprint
    model, which ``fetch`` runs and a launch otherwise downloads mid-call."""
    proc = run(
        [sys.executable, "-c", _EXTRAS],
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        timeout=STEP_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        notes.append(
            "camoufox's add-ons or fingerprint model did not install; a launch fetches them"
        )


def _chromium_step(run: Runner, notes: list[str]) -> tuple[str, bool, list[Path]]:
    if packages.installed_versions().get("patchright") is None:
        return "skipped (no patchright)", True, []
    package_dir = chromium.package_dir("patchright")
    revision, builds = chromium.install_dirs(package_dir) if package_dir else (None, {})
    dirs = list(builds.values())
    if revision is None:
        notes.append("patchright's browsers.json could not be read")
        return "unknown", False, []
    # Playwright deletes revisions it thinks unused; one a live browser runs from must stay.
    extra = {"PLAYWRIGHT_SKIP_BROWSER_GC": "1"} if _live_chromium() else {}
    run(
        [sys.executable, "-m", "patchright", "install", "chromium"],
        env=_child_env(**extra),
        stdin=subprocess.DEVNULL,
        timeout=STEP_TIMEOUT_S,
        check=False,
    )
    unfinished = [d for d in dirs if not (d / "INSTALLATION_COMPLETE").is_file()]
    if unfinished:
        notes.append("not installed: " + ", ".join(str(d) for d in unfinished))
        return "missing", False, []
    binaries = [b for d in dirs for b in sorted(d.glob("chrome*/chrome")) if b.is_file()]
    return revision, True, binaries


def _missing_libs(run: Runner, binaries: list[Path]) -> list[str]:
    missing: set[str] = set()
    for binary in binaries:
        # A browser ships its own libraries beside the binary and puts that
        # directory on the library path at launch; only the rest are system's.
        paths = [
            str(binary.parent),
            *filter(None, os.environ.get("LD_LIBRARY_PATH", "").split(":")),
        ]
        env = {**os.environ, "LD_LIBRARY_PATH": ":".join(paths)}
        proc = run(
            ["ldd", str(binary)], env=env, capture_output=True, text=True, timeout=120, check=False
        )
        for line in (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines():
            if "=> not found" in line:
                missing.add(line.split("=>", 1)[0].strip())
            elif "not found (required by" in line:  # e.g. version `GLIBC_2.38' not found
                missing.add(line.split("not found", 1)[0].split(":")[-1].strip())
    return sorted(missing)


def _libs_step(run: Runner, binaries: list[Path], notes: list[str]) -> tuple[str, bool]:
    if not binaries:
        return "unchecked", True
    missing = _missing_libs(run, binaries)
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
        run(["sudo", "-n", *deps], stdin=subprocess.DEVNULL, timeout=STEP_TIMEOUT_S, check=False)
        missing = _missing_libs(run, binaries)
        if not missing:
            return "ok", True
        notes.append(
            "install-deps did not supply every library; if it was interrupted, "
            "`sudo dpkg --configure -a` finishes the package install"
        )
    notes.append(f"operator step: `sudo {' '.join(deps)}`")
    return "missing:" + ",".join(missing), False


def _already_ready() -> bool:
    """Whether the engine resolves and patchright's Chromium (when patchright is
    installed) is complete, so a run would download nothing."""
    if not engine.resolve_camoufox_engine().ready:
        return False
    if packages.installed_versions().get("patchright") is None:
        return True
    return all(chromium.not_ready("patchright", headless=h) is None for h in (False, True))


def provision(run: Runner | None = None) -> Outcome:
    """Bring the browser stack to ready; idempotent. ``run`` defaults to
    ``subprocess.run``, looked up at call time."""
    run = run or subprocess.run
    notes: list[str] = []
    root = engine.camoufox_install_dir()
    need, free = min_free_bytes(), free_bytes(root)
    if free < need:
        # Success only when nothing needed downloading: after the browser packages
        # moved to a new pin, a skip leaves the browser unusable, and a unit that
        # reads as succeeded would hide that every day the timer repeats it.
        return Outcome(
            f"skipped (low disk: {free / 1024**3:.1f} GB free, {need / 1024**3:.1f} GB needed)",
            "skipped",
            "unchecked",
            _already_ready(),
        )
    root.mkdir(parents=True, exist_ok=True)
    _clear_stale_temp()
    if mark_legacy(root):
        notes.append(f"marked the pre-0.5 engine in {root} so camoufox 0.5 keeps it")
    try:
        eng, eng_ok, status = _engine_step(run, notes)
    except Exception as exc:  # noqa: BLE001 - reported on the outcome line
        notes.append(f"engine step failed: {exc!r}")
        eng, eng_ok, status = f"error:{type(exc).__name__}", False, None
    if status is not None and status.ready and _pin_capable():
        try:
            _extras_step(run, notes)
        except Exception as exc:  # noqa: BLE001 - a launch fetches them itself
            notes.append(f"add-on / fingerprint model step failed: {exc!r}")
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


def _browser_requirements() -> list:
    """The ``browser`` extra's requirements as the installed Genesis records them
    (its Requires-Dist), so pyproject.toml stays their one source. A requirement
    belongs to the extra when its marker holds with ``extra == "browser"`` and not
    without it (a core one carrying, say, a python_version marker is not)."""
    from packaging.requirements import Requirement

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


def unmet_packages() -> list[str]:
    """What the installed browser packages lack against the ``browser`` extra:
    each package installed, at a version its requirement admits, and camoufox
    carrying its browser pin. Read by bootstrap and install.sh after pip, whose
    exit status they cannot see."""
    versions = packages.installed_versions()
    unmet = [f"{dist} is not installed" for dist in packages.BROWSER_DISTS if not versions[dist]]
    try:
        requirements = _browser_requirements()
    except Exception as exc:  # noqa: BLE001 - no packaging, or unreadable metadata
        unmet.append(f"the browser extra's version ranges could not be read ({exc!r})")
        requirements = []
    for req in requirements:
        installed = versions.get(req.name.lower())
        if installed and not req.specifier.contains(installed, prereleases=True):
            unmet.append(f"{req.name} {installed} is outside {req.specifier}")
    if versions["camoufox"] and not _pin_capable():
        unmet.append(f"camoufox {versions['camoufox']} predates the browser pin (0.5.7)")
    return unmet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m genesis.browser.provision")
    parser.add_argument(
        "command", choices=("run", "mark-legacy", "check-packages"), nargs="?", default="run"
    )
    args = parser.parse_args(argv)
    if args.command == "mark-legacy":
        mark_legacy(engine.camoufox_install_dir())
        return 0
    if args.command == "check-packages":
        unmet = unmet_packages()
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
