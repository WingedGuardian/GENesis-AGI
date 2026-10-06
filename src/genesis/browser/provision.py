"""Provision the browser stack: the ``browser`` extra, the Camoufox engine, safe upgrades.

``python -m genesis.browser.provision run --root <repo> --lib <venv_setup.sh>`` is
the whole transaction, in ONE process so nothing it learns has to be passed
between steps as text. scripts/install_browser_stack.sh calls it. It never fails
its caller: problems print, the outcome line is printed last, and the
``browser_automation`` capability reports the end state.

Steps, in order (all under the browser-stack lock held EXCLUSIVE, so two runs
never overlap and no browser launches mid-run; a running browser holds it SHARED):
  1. preflight: enough disk, and no browser running (an upgrade under a live
     browser would swap its engine and copy its profile mid-write). Anything
     it cannot determine counts as "not safe": skip and say why. The installed
     camoufox/playwright/patchright versions are recorded in memory.
  2. install the ``browser`` extra through the worktree-guarded installer in
     scripts/lib/venv_setup.sh (its one home), then check the packages import,
     that camoufox carries a browser pin, and that every installed version
     satisfies the extra's ranges (the installer masks pip's exit status).
  3. copy the Camoufox profile if a newer Firefox major is about to open it
     (Firefox refuses a profile last used by a newer version).
  4. the engine. A ``camoufox set`` choice of another build is reset to the
     paired one first. Already ready: nothing to do. An install root in
     camoufox 0.5's side-by-side layout: ``camoufox fetch`` in place (it adds a
     version and deletes nothing). Anything else (a pre-0.5 engine, the residue
     of an interrupted download, no root at all): fetch into a staging directory
     next to the root, check the staged engine and launch it from there, then
     swap: the pre-0.5 engine is renamed to ``<root>.pre-0.5-<date>`` (residue
     is deleted) and the staged engine is renamed into place. camoufox 0.5's
     own fetch deletes a pre-0.5 root BEFORE downloading; staging keeps the old
     engine in place for the whole download, so only the two renames are not
     atomic, and signals are held off while they run.
  5. launch Camoufox headless once, with the new packages.
  6. refresh ``browser_automation`` in ~/.genesis/capabilities.json, so the
     capability reflects the new state without a server restart.
If step 2, 3, 4 or 5 fails (or the run is interrupted by SIGTERM, SIGINT or SIGHUP)
before the swap, the root was never touched, so the only thing to undo is the
packages: the versions recorded in step 1 are reinstalled, any browser package
the step added is removed, and the old engine works again. Every step runs in
its own process group, and an interrupt stops the whole group before the
rollback starts; if a member survives even SIGKILL, the rollback is refused
rather than run beside it.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import datetime as _dt
import fcntl
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from genesis.browser.engine import (
    BROWSER_LOCK_FILE,
    OVERRIDDEN,
    EngineStatus,
    camoufox_engine_status,
    camoufox_install_dir,
    camoufox_pin,
)


# MEASURED 2026-10-04 on a sandboxed live upgrade: the Camoufox 156 engine is a
# 1.3 GB download that unpacks to 2.4 GB (both exist briefly), patchright's
# Chromium + headless shell + ffmpeg came to 0.66 GB, and each profile backup is
# the profile's size (0.43 GB there). About 5 GB peak, hence 6 GB with margin.
# Checked on the cache filesystem (engines, and the engine staging dir next to
# them) and the home filesystem (profiles).
def _min_free_bytes() -> int:
    try:
        gb = float(os.environ.get("GENESIS_BROWSER_MIN_FREE_GB", "6"))
    except ValueError:
        gb = 6.0
    return int(gb * 1024**3)


MIN_FREE_BYTES = _min_free_bytes()

# No hang has been observed; this is the project's default floor for an
# unattended long step (genesis-development, Timeout Policy). A 1.3 GB download
# on a slow link legitimately takes tens of minutes.
STEP_TIMEOUT_S = 7200
# The launch smoke test is the exception, with a named failure: a browser that
# cannot start (missing system libraries, a wedged display) hangs instead of
# exiting, and the install must not hang with it. A healthy launch measured a
# few seconds; five minutes is two orders of magnitude of slack.
SMOKE_TIMEOUT_S = 300

# Downloads are staged here, never in the default temp dir: inside a Claude Code
# session TMPDIR is the shared, quota-capped cc-tmp volume (2 GB measured), and
# a 1.3 GB engine download there breaks every session at once.
STAGING_PARENT = Path.home() / "tmp"
PROFILE_DIR = Path.home() / ".genesis" / "camoufox-profile"
LOCK_FILE = BROWSER_LOCK_FILE
CAPABILITIES_FILE = Path.home() / ".genesis" / "capabilities.json"
CAPABILITY = "browser_automation"

# The packages whose versions are recorded before the install and put back if
# the transaction fails.
PACKAGES = ("camoufox", "playwright", "patchright")
_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class ProvisionError(RuntimeError):
    """A step failed; the transaction puts the previous packages back."""


class StepSurvived(ProvisionError):
    """A step's process group still has members after SIGKILL.

    Such a member (pip in uninterruptible I/O, say) may still be writing the
    venv, so nothing may write there beside it: the package restore is refused.
    """


def _say(msg: str) -> None:
    # Best-effort: bootstrap and install.sh pipe this through `sed`, which a
    # Ctrl-C kills too. A BrokenPipeError raised here inside the failure handler
    # used to skip the package rollback that follows it (measured).
    global _output_broken
    try:
        print(f"  {msg}", flush=True)
    except OSError:
        _output_broken = True


def _say_err(text: str) -> None:
    global _output_broken
    try:
        print(text, file=sys.stderr, flush=True)
    except OSError:
        _output_broken = True


# Set once our output is found broken: later steps (the rollback's pip) then
# write to /dev/null instead of a dead pipe, where a write error could fail them.
_output_broken = False


def _stamp() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%d")


def installed_versions() -> dict[str, str]:
    """Installed version of each of PACKAGES that is present."""
    found: dict[str, str] = {}
    for name in PACKAGES:
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return found


def unmet_browser_requirements(pyproject: Path) -> list[str]:
    """Each requirement of the ``browser`` extra that the installed packages miss.

    Read from the repo's pyproject.toml, not the installed metadata, which a
    failed install leaves at the previous version.
    """
    import tomllib

    try:
        from packaging.requirements import Requirement
    except ImportError:
        # Not a declared dependency (measured absent in a minimal venv); pip,
        # which this transaction already requires, vendors the same module.
        from pip._vendor.packaging.requirements import Requirement

    data = tomllib.loads(pyproject.read_text())
    extra = data.get("project", {}).get("optional-dependencies", {}).get("browser", [])
    unmet: list[str] = []
    for line in extra:
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate({"extra": "browser"}):
            continue
        try:
            have = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            unmet.append(f"{req} (not installed)")
            continue
        if not req.specifier.contains(have, prereleases=True):
            unmet.append(f"{req} (installed {have})")
    return unmet


# ── preflight ─────────────────────────────────────────────────────────────


def _pids_named(name: str) -> list[int] | None:
    """PIDs whose process NAME is exactly ``name``; None if pgrep cannot answer."""
    try:
        proc = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        return None
    return [int(p) for p in proc.stdout.split() if p.isdigit()]


def camoufox_running() -> bool | None:
    """Whether a Camoufox browser runs (process name ``camoufox-bin``); None if unknown."""
    pids = _pids_named("camoufox-bin")
    return None if pids is None else bool(pids)


def _is_playwright_chromium(exe: str) -> bool:
    if "ms-playwright" in exe or os.path.basename(exe).startswith("chrome-headless"):
        return True
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    return bool(custom) and os.path.isabs(custom) and exe.startswith(custom.rstrip("/") + "/")


def chromium_running() -> bool | None:
    """Whether a Playwright/patchright Chromium runs; None if unknown.

    A ``chrome`` process counts only when its executable lives under a Playwright
    browsers directory (``ms-playwright``, or ``$PLAYWRIGHT_BROWSERS_PATH``), so a
    desktop Chrome does not block the upgrade. chrome-headless-shell's process
    name is cut to 15 characters by the kernel, hence ``chrome-headless``.
    """
    chrome = _pids_named("chrome")
    headless = _pids_named("chrome-headless")
    if chrome is None or headless is None:
        return None
    for pid in chrome + headless:
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except FileNotFoundError:
            continue  # exited between pgrep and the read
        except PermissionError:
            continue  # another user's process: it cannot hold our profile or engine
        except OSError:
            return None
        if _is_playwright_chromium(exe):
            return True
    return False


def browsers_running() -> bool | None:
    """True/False, or None when it cannot be determined (treated as running).

    Matches process NAMES, never command-line substrings: ``pgrep -f camoufox-bin``
    also matches any shell or grep whose arguments merely mention the word, which
    measured as a false "running" on a live run. This is deliberately stricter
    than the ``pgrep -f`` patterns in genesis.browser.types.
    """
    camoufox = camoufox_running()
    chromium = chromium_running()
    if camoufox is None or chromium is None:
        return None
    return camoufox or chromium


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def preflight() -> str | None:
    """None when it is safe to proceed, else the reason to skip."""
    filesystems = (
        ("cache", camoufox_install_dir().parent),
        ("home", Path.home()),
        ("staging", STAGING_PARENT),
    )
    for label, path in filesystems:
        free = _free_bytes(path)
        if free < MIN_FREE_BYTES:
            return (
                f"{free / 1024**3:.1f} GB free on the {label} filesystem ({path}), needs "
                f"{MIN_FREE_BYTES / 1024**3:.0f} GB (GENESIS_BROWSER_MIN_FREE_GB overrides)"
            )
    running = browsers_running()
    if running is None:
        return "could not tell whether a browser is running (pgrep unavailable or failed)"
    if running:
        return (
            "a browser is running; re-run scripts/install_browser_stack.sh "
            "when no browser session is active"
        )
    return None


# ── backups ───────────────────────────────────────────────────────────────


def copy_aside(src: Path, label: str) -> Path:
    """Copy ``src`` to ``<src>.pre-<label>-<timestamp>``, replacing older copies.

    Called only while the profile still records the OLD version, so the newest
    copy is the one a rollback needs: after an upgrade that failed before the
    newer browser opened the profile, the user kept using it, and a copy from the
    first attempt would lose everything since. Older copies with the same label
    are removed only after the new one is complete. The copy is built inside a
    TemporaryDirectory next to ``src`` and renamed out of it when complete, so an
    interrupted copy is never taken for a finished one and any failure removes
    it; a run killed outright leaves a ``*.tmp`` directory that
    scripts/disk_hygiene.sh removes after a day. The copy is stamped
    with today's time so retention ages it from today.
    """
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    older = [p for p in src.parent.glob(f"{src.name}.pre-{label}-*") if not p.name.endswith(".tmp")]
    dest = src.with_name(f"{src.name}.pre-{label}-{stamp}")
    n = 1
    while dest.exists():  # a second copy within the same second
        dest = src.with_name(f"{src.name}.pre-{label}-{stamp}-{n}")
        n += 1
    with tempfile.TemporaryDirectory(
        dir=src.parent, prefix=f"{dest.name}.", suffix=".tmp", ignore_cleanup_errors=True
    ) as work:
        copied = Path(work) / src.name
        shutil.copytree(src, copied, symlinks=True)
        # copytree keeps the profile's own mtime, which is its last browser
        # session and can be older than the 14-day retention; stamped before the
        # rename, so a finished backup always ages from today.
        os.utime(copied)
        copied.rename(dest)
    for stale in older:
        if stale != dest:
            shutil.rmtree(stale, ignore_errors=True)
    return dest


def firefox_profile_major(profile: Path) -> int | None:
    """Major Firefox version that last opened ``profile`` (compatibility.ini)."""
    parser = configparser.ConfigParser()
    try:
        if not parser.read(profile / "compatibility.ini"):
            return None
        return int(parser.get("Compatibility", "LastVersion").split(".", 1)[0])
    except (configparser.Error, ValueError):
        return None


def _major(version: str) -> int | None:
    """Leading number of a version string; None when it has none (back it up)."""
    try:
        return int(version.split(".", 1)[0])
    except ValueError:
        return None


def _running_check(kind: str) -> Callable[[], bool | None]:
    # Looked up at call time so tests can patch the module attributes.
    if kind == "Camoufox":
        return camoufox_running
    if kind == "Chromium":
        return chromium_running
    return browsers_running


def backup_if_upgrading(
    profile: Path, last_major: int | None, new_major: int | None, kind: str
) -> None:
    """Copy ``profile`` once before a newer major first opens it.

    Stateless: the trigger is the profile's own record of the version that last
    opened it, so once the new version has opened it no further copy is made.
    An unreadable version on either side means "back it up": a missed copy of a
    one-way profile upgrade cannot be recovered, an extra copy can be pruned.
    """
    if not profile.is_dir() or not any(profile.iterdir()):
        return
    if last_major is not None and new_major is not None and last_major >= new_major:
        return
    # Re-check right before copying: the preflight answer is minutes old by now.
    # Only the browser that owns this profile matters here.
    if _running_check(kind)() is not False:
        raise ProvisionError(f"a browser may be using the {kind} profile; not copying it")
    label = f"v{last_major}" if last_major is not None else "vunknown"
    backup = copy_aside(profile, label)
    _say(f"backed up the {kind} profile (last opened by {label}) to {backup}")


# ── capability ────────────────────────────────────────────────────────────


def _default_capability_description() -> str:
    try:
        from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

        return _CAPABILITY_DESCRIPTIONS.get(CAPABILITY, CAPABILITY)
    except Exception:  # noqa: BLE001 - a description is never worth failing for
        return CAPABILITY


def refresh_capability(status: EngineStatus, launched: bool | None = None) -> None:
    """Set ``browser_automation`` in capabilities.json from ``status``; never raises.

    ``launched=False`` (the launch check failed) reports it degraded even when
    the engine's files are in place.

    The server writes the file at startup; without this the capability would
    report the pre-upgrade state until the next restart. Skipped when the file
    does not exist (no server has run yet; it will write the real state).
    """
    try:
        path = CAPABILITIES_FILE
        if not path.is_file():
            return
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return
        entry = data.get(CAPABILITY)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry["status"] = "active" if status.ready and launched is not False else "degraded"
        if not entry.get("description"):
            entry["description"] = _default_capability_description()
        entry.pop("error", None)
        data[CAPABILITY] = entry
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".capabilities-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
        _say(f"note: could not update {CAPABILITY} in capabilities.json ({exc})")


# ── the transaction ───────────────────────────────────────────────────────


@contextlib.contextmanager
def _signals_held():
    """Defer SIGTERM/SIGINT/SIGHUP across a few steps that must not be split."""
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, _SIGNALS)
    except (AttributeError, OSError, ValueError):
        yield
        return
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


_GROUP_GRACE_S = 10  # how long a step's processes get to exit on SIGTERM before SIGKILL


def _kill_group(proc: subprocess.Popen) -> bool:
    """Stop every process in a step's group; True once none is left.

    start_new_session makes the step a group leader, so pgid == pid. The leader
    is reaped inside the poll: an unreaped zombie keeps the group visible to
    killpg(pgid, 0), which made every interrupt wait out both grace periods
    (measured 21 s). Once the leader is reaped, ESRCH means no member is left.

    False means a member outlived SIGKILL's grace period too (a process in
    uninterruptible I/O ignores SIGKILL until the I/O returns), or the group was
    never signalled because its id is init's or our own. Either way the group may
    still be writing, and the caller must not act as though it were gone.
    """
    pgid = proc.pid
    if pgid <= 1 or pgid == os.getpgrp():
        return False  # never signal init's group or our own
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + _GROUP_GRACE_S
        while time.monotonic() < deadline:
            proc.poll()
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
    return False


def _run_group(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    capture_output: bool = False,
    text: bool = False,
    pass_fds: tuple[int, ...] = (),
    on_survived: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess:
    """subprocess.run, but an interrupt or timeout stops the step's WHOLE tree.

    subprocess.run kills only its direct child. A step is often a wrapper (bash
    running pip), so killing the wrapper left pip writing site-packages while the
    rollback started a second pip on the same files. Each step gets its own
    session, and any exception while waiting (the signal handler's
    ProvisionError, TimeoutExpired) stops the group before it propagates. When
    the group does not stop, ``on_survived(pgid)`` is called while signals are
    still held (so a second signal cannot lose it) and StepSurvived replaces that
    exception, so the caller knows the step may still be running.

    Its own session also means a step can outlive a parent killed by a signal
    nothing handles (SIGKILL, SIGQUIT). ``pass_fds`` carries the provisioning
    lock into the step: a flock belongs to the open file, so the lock stays held
    while any step process lives and a re-run cannot start a second pip.
    """
    inherited = subprocess.DEVNULL if _output_broken else None
    pipe = subprocess.PIPE if capture_output else inherited
    with subprocess.Popen(
        cmd,
        env=env,
        stdout=pipe,
        stderr=pipe,
        text=text,
        start_new_session=True,
        pass_fds=pass_fds,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException as exc:
            # A second signal must not cut the cleanup short and leave the step
            # running: hold them until the group is gone.
            with _signals_held():
                gone = _kill_group(proc)
                if gone:
                    proc.wait()
                elif on_survived is not None:
                    on_survived(proc.pid)
            if not gone:
                raise StepSurvived(
                    f"`{' '.join(cmd[:4])}` (process group {proc.pid}) still has "
                    f"processes after SIGKILL"
                ) from exc
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class Transaction:
    def __init__(self, root: Path, lib: Path, *, install: bool = True) -> None:
        self.repo_root = root
        self.lib = lib
        self.install = install
        self.engine_root = camoufox_install_dir()
        self.previous: dict[str, str] = {}
        self.swapped = False
        self.interrupted = False
        self.step_survived = False
        self.rolling_back = False
        self.tmpdir: Path | None = None
        self.env = dict(os.environ)
        self.lock_fd: int | None = None

    def _check_interrupt(self) -> None:
        """Raise for a signal that a catch-all swallowed.

        The handler raises once, and camoufox_engine_status (by design) never
        raises: a signal landing inside a status read is caught there and only
        ``self.interrupted`` remembers it. Checked before every step and before
        the swap, so such a signal still stops the run. Not during the rollback,
        whose pip must run whatever arrives.
        """
        if self.interrupted and not self.rolling_back:
            raise ProvisionError("interrupted by a signal")

    def _run(self, cmd: list[str], *, env: dict[str, str] | None = None, **kw):
        self._check_interrupt()
        fds = (self.lock_fd,) if self.lock_fd is not None else ()
        try:
            return _run_group(
                cmd,
                env=env if env is not None else self.env,
                pass_fds=fds,
                on_survived=self._record_survivor,
                **kw,
            )
        except StepSurvived:
            self.step_survived = True
            raise

    def _record_survivor(self, _pgid: int) -> None:
        self.step_survived = True

    # step 2
    def install_extras(self) -> None:
        if not self.install:
            return
        venv = Path(sys.prefix)
        proc = self._run(
            [
                "bash",
                "-c",
                '. "$1"; editable_install_guarded "$2" "$3" browser',
                "_",
                str(self.lib),
                str(self.repo_root),
                str(venv),
            ],
            timeout=STEP_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise ProvisionError(f"installing the [browser] extra failed (rc={proc.returncode})")
        # The guarded installer only checks that Genesis imports; check the extra.
        check = self._run(
            [sys.executable, "-c", "import camoufox, playwright, patchright"],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            raise ProvisionError(
                f"the [browser] packages do not import: {check.stderr.strip()[-300:]}"
            )
        # The import check passes against an old camoufox too; 0.5.7+ ships a pin.
        if camoufox_pin() is None:
            raise ProvisionError(
                "the [browser] extra did not install camoufox 0.5.7+ (no browser pin)"
            )
        # The guarded installer masks pip's exit status, and an older 0.5-era
        # stack passes both checks above: compare against the extra itself.
        unmet = unmet_browser_requirements(self.repo_root / "pyproject.toml")
        if unmet:
            raise ProvisionError(
                f"the [browser] extra is not satisfied after install: {'; '.join(unmet)}"
            )

    # steps 3 and 4
    def engine(self) -> None:
        pin = camoufox_pin()
        if pin:
            backup_if_upgrading(
                PROFILE_DIR,
                firefox_profile_major(PROFILE_DIR),
                _major(pin.version),
                "Camoufox",
            )
        status = camoufox_engine_status()
        if status.state == OVERRIDDEN:
            # A bare `camoufox fetch` would fetch the overriding build, which the
            # status (and so the launch guard) never accepts: it cannot converge.
            _say(f"resetting to the paired Camoufox build: {status.detail}")
            proc = self._run(
                [sys.executable, "-m", "camoufox", "set", "--release"], timeout=STEP_TIMEOUT_S
            )
            if proc.returncode != 0:
                raise ProvisionError(f"`camoufox set --release` exited {proc.returncode}")
            status = camoufox_engine_status()
        if status.ready:
            _say(f"Camoufox engine up to date ({status.detail})")
            return
        root = self.engine_root
        if (root / ".0.5_FLAG").exists():
            # camoufox 0.5's layout keeps versions side by side and its fetch
            # deletes nothing here, so fetching in place is safe.
            self._fetch(self.env)
            status = camoufox_engine_status()
            if not status.ready:
                raise ProvisionError(
                    f"the Camoufox engine is not ready after fetch: {status.detail}"
                )
            _say(f"Camoufox engine installed ({status.detail})")
            return
        self._stage_and_swap()

    def _fetch(self, env: dict[str, str]) -> None:
        _say("fetching the pinned Camoufox engine (1.3 GB download, 2.4 GB unpacked)...")
        try:
            self._run([sys.executable, "-m", "camoufox", "fetch"], env=env, timeout=STEP_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise ProvisionError(f"camoufox fetch did not finish in {STEP_TIMEOUT_S}s") from exc
        # The exit code is not trusted: with no network, fetch has exited 0
        # having installed nothing (measured). The engine status is the test.

    def _stage_and_swap(self) -> None:
        root = self.engine_root
        # Same parent, so the same filesystem: the swap is two renames.
        staging = root.parent / f".camoufox-staging-{os.getpid()}"
        try:
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            # camoufox puts its engines in platformdirs.user_cache_dir, which
            # honours an absolute XDG_CACHE_HOME; its config stores relative paths.
            self._fetch(dict(self.env, XDG_CACHE_HOME=str(staging)))
            staged = staging / "camoufox"
            status = camoufox_engine_status(install_dir=staged)
            if not status.ready:
                raise ProvisionError(
                    f"the Camoufox engine is not ready after fetch: {status.detail}"
                )
            # Files present is not "usable": launch the staged engine before it
            # replaces the working one, which stays until this passes.
            if not self.smoke(env=dict(self.env, XDG_CACHE_HOME=str(staging)), install_dir=staged):
                raise ProvisionError(
                    "the staged Camoufox engine did not launch; the previous engine is untouched"
                )
            self._check_interrupt()
            with _signals_held():
                self._swap_in(staged)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        _say(f"Camoufox engine installed ({camoufox_engine_status().detail})")

    def _swap_in(self, staged: Path) -> None:
        root = self.engine_root
        moved: Path | None = None
        if root.exists() or root.is_symlink():
            if (root / "version.json").is_file():
                moved = root.with_name(f"{root.name}.pre-0.5-{_stamp()}")
                if moved.exists():
                    moved = moved.with_name(f"{moved.name}-{os.getpid()}")
                root.rename(moved)
            elif root.is_dir() and not root.is_symlink():
                shutil.rmtree(root)  # residue of an interrupted download; nothing to keep
            else:
                root.unlink()
        try:
            staged.rename(root)
        except OSError:
            if moved is not None:
                moved.rename(root)  # put the old engine back before reporting
            raise
        self.swapped = True
        if moved is not None:
            os.utime(moved)  # rename keeps the old mtime; retention ages it from today
            _say(f"moved the pre-0.5 Camoufox engine aside to {moved}")

    def restore_packages(self) -> None:
        """Reinstall the versions recorded before the install, if they changed."""
        if self.step_survived:
            # pip (or its wrapper) may still be writing the venv; a second pip
            # beside it would interleave writes to the same files.
            raise ProvisionError(
                "a step's processes outlived SIGKILL and may still be writing the venv, "
                f"so the previous browser packages were NOT restored (their temp dir "
                f"{self.tmpdir} is kept); re-run scripts/install_browser_stack.sh once "
                f"they exit (until then it reports the lock as held)"
            )
        previous = self.previous
        if "camoufox" not in previous or self.swapped:
            # Nothing to go back to, or the new engine is already in place and the
            # new packages are the ones that match it.
            return
        current = installed_versions()
        changed = any(current.get(name) != version for name, version in previous.items())
        # A package the step ADDED is removed too: patchright left beside the
        # restored playwright would be preferred by the Chromium fallback while
        # its Chromium was never installed (measured in the live harness).
        added = [name for name in PACKAGES if name not in previous and name in current]
        if changed:
            pins = [f"{name}=={previous[name]}" for name in PACKAGES if name in previous]
            _say(f"restoring the previous browser packages: {' '.join(pins)}")
            proc = self._run(
                [sys.executable, "-m", "pip", "install", "--quiet", *pins],
                timeout=STEP_TIMEOUT_S,
            )
            if proc.returncode != 0:
                raise ProvisionError(
                    f"pip exited {proc.returncode} restoring {' '.join(pins)}; run "
                    f"`{sys.executable} -m pip install {' '.join(pins)}` by hand"
                )
            _say(f"restored {' '.join(pins)}; the previous Camoufox engine was never touched")
        if added:
            _say(f"removing browser packages this run added: {' '.join(added)}")
            proc = self._run(
                [sys.executable, "-m", "pip", "uninstall", "--yes", "--quiet", *added],
                timeout=STEP_TIMEOUT_S,
            )
            if proc.returncode != 0:
                raise ProvisionError(
                    f"pip exited {proc.returncode} removing {' '.join(added)}; run "
                    f"`{sys.executable} -m pip uninstall {' '.join(added)}` by hand"
                )

    # step 5
    def smoke(self, env: dict[str, str] | None = None, install_dir: Path | None = None) -> bool:
        status = (
            camoufox_engine_status(install_dir=install_dir)
            if install_dir is not None
            else camoufox_engine_status()
        )
        if not status.ready:
            return False  # never let the smoke launch trigger camoufox's own download
        try:
            proc = self._run(
                [sys.executable, "-c", _SMOKE],
                env=env,
                capture_output=True,
                text=True,
                timeout=SMOKE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            _say(f"FAILED: Camoufox did not launch within {SMOKE_TIMEOUT_S}s")
            return False
        if proc.returncode != 0:
            _say("FAILED: Camoufox did not launch. Missing system libraries are the usual cause:")
            for line in (proc.stderr or proc.stdout).strip().splitlines()[-3:]:
                _say(f"    {line}")
            return False
        return True

    def run(self) -> str:
        """Run every step under one lock; returns the outcome line (printed last)."""
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOCK_FILE, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Browser launches hold this lock SHARED for their lifetime, and a
                # provisioning run holds it EXCLUSIVE: tell the two apart.
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    # The run holding the lock refreshes the capability itself.
                    return self._finish(
                        "SKIPPED (another provisioning run holds the lock)", refresh=False
                    )
                fcntl.flock(lock, fcntl.LOCK_UN)
                return self._finish(
                    "SKIPPED (a Claude Code session has a browser open; it holds the "
                    "browser lock until the browser closes)"
                )
            self.lock_fd = lock.fileno()
            try:
                return self._run_locked()
            finally:
                self.lock_fd = None

    def _clear_stale_staging(self) -> None:
        # Under the lock no other run is staging, so any staging dir is left from
        # a run that was killed outright (SIGKILL, power loss): about 2.4 GB.
        for stale in self.engine_root.parent.glob(".camoufox-staging-*"):
            shutil.rmtree(stale, ignore_errors=True)

    def _run_locked(self) -> str:
        # Before the free-space check: a killed run's ~2.4 GB stage would
        # otherwise keep every later run under the floor.
        self._clear_stale_staging()
        reason = preflight()
        if reason:
            return self._finish(f"SKIPPED ({reason})")
        self.previous = installed_versions()
        STAGING_PARENT.mkdir(parents=True, exist_ok=True)
        self.tmpdir = Path(tempfile.mkdtemp(prefix="genesis-browser-", dir=STAGING_PARENT))
        self.env["TMPDIR"] = str(self.tmpdir)

        def _interrupted(signum, _frame):
            # One-shot: a later signal (the operator's second Ctrl-C while the
            # first one's step is still being stopped) must not cut that cleanup
            # short, nor reach into the rollback before its handlers are swapped.
            first = not (self.interrupted or self.rolling_back)
            self.interrupted = True
            if first:
                raise ProvisionError(f"interrupted by signal {signal.Signals(signum).name}")

        def _recorded(signum, _frame):
            self.interrupted = True

        previous_handlers = {sig: signal.signal(sig, _interrupted) for sig in _SIGNALS}
        engine_ok = smoke_ok = False
        try:
            try:
                self.install_extras()
                self.engine()
                engine_ok = True
                # Inside the rollback on every engine path (already ready, fetched
                # in place, or staged and swapped): new packages that cannot
                # launch Camoufox go back, unless the swap has already happened.
                if not self.smoke():
                    raise ProvisionError("Camoufox did not launch with the installed packages")
                smoke_ok = True
            except Exception as exc:  # noqa: BLE001 - every failure is handled the same way
                # From here on a signal is recorded, never raised: a second Ctrl-C
                # must not kill the rollback's pip midway through a write.
                self.rolling_back = True
                for sig in _SIGNALS:
                    signal.signal(sig, _recorded)
                _say(f"FAILED: {exc}")
                _say_err(traceback.format_exc())
                try:
                    self.restore_packages()
                except Exception as rb_exc:  # noqa: BLE001 - report, never raise
                    _say(f"FAILED to restore the previous browser packages: {rb_exc}")
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            if not self.step_survived:  # a surviving step may still be using it
                shutil.rmtree(self.tmpdir, ignore_errors=True)
        try:
            now = installed_versions()
        except Exception:  # noqa: BLE001 - a notice must never fail the run
            now = self.previous
        if now != self.previous:
            # A running Claude Code session keeps the old modules imported; its
            # browser tools fail on the new camoufox until it restarts.
            _say(
                "browser packages changed; restart running Claude Code sessions "
                "before using their browser tools"
            )
        state = "ready" if (engine_ok and smoke_ok) else "DEGRADED"
        return self._finish(
            f"{state} (engine={engine_ok}, launch={smoke_ok})",
            launched=smoke_ok if engine_ok else None,
        )

    @staticmethod
    def _finish(result: str, *, refresh: bool = True, launched: bool | None = None) -> str:
        status = camoufox_engine_status()
        if refresh:
            refresh_capability(status, launched)
        if not status.ready:
            usable = "Camoufox DOWN until this is re-run"
        elif launched is False:
            usable = "Camoufox installed but did not launch"
        else:
            usable = "Camoufox usable"
        outcome = f"browser stack: {result}; {usable}: {status.detail}"
        _say(outcome)
        return outcome


_SMOKE = """
import asyncio
from camoufox.async_api import AsyncCamoufox

async def main():
    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        await page.goto("about:blank")

asyncio.run(main())
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m genesis.browser.provision")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", required=True, type=Path, help="Genesis repo root")
    run.add_argument("--lib", required=True, type=Path, help="scripts/lib/venv_setup.sh")
    run.add_argument(
        "--no-install",
        action="store_true",
        help="skip the package install (test harness: packages installed separately)",
    )
    args = parser.parse_args(argv)
    Transaction(args.root, args.lib, install=not args.no_install).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
