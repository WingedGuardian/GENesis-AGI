"""The Chromium fallback's step of the browser-stack provisioning transaction.

genesis.browser.provision runs :func:`provision` after the Camoufox engine is in
place and launches. The step is non-fatal (the Chromium fallback is not the
primary layer), so it never rolls the browser packages back:

  1. ``patchright install chromium``;
  2. copy the Chromium profile once before a different Chromium build first
     opens it (the caller's ``backup`` does the copy); a profile a NEWER build
     wrote is left alone, never opened by the older one;
  3. launch Chromium exactly as the fallback does (headed, on its persistent
     profile, on the VNC display, with its flags). When it does not start,
     install its Linux system libraries with ``patchright install-deps
     chromium`` through non-interactive sudo, or print the command to run.

Every subprocess goes through the caller's ``run`` (the transaction's process
group runner, which carries the provisioning lock and stops the step's whole
tree on an interrupt). One limit stated: an interrupt during ``sudo ...
install-deps`` signals sudo's process group; apt may run in a session of its own
under sudo's pty and outlive it, leaving dpkg to be finished with
``dpkg --configure -a``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

PROFILE_DIR = Path.home() / ".genesis" / "browser-profile"
# The fallback's launch (mcp/health/browser.py _ensure_chromium_fallback), shared
# so the check below cannot drift from it: headed on the VNC display, with these
# flags, on the persistent profile.
DISPLAY = ":99"
LAUNCH_ARGS = ("--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--start-maximized")

# The launch check runs the fallback's own launch, so it fails where the
# fallback would: a headless launch on a fresh profile runs
# chromium-headless-shell (fewer system libraries), needs no X display, and
# never opens the persistent profile. It runs after the profile backup, so the
# profile it opens is the one already copied. The profile path is argv[1].
SMOKE = f"""
import asyncio, os, sys
from patchright.async_api import async_playwright

async def main():
    os.environ["DISPLAY"] = {DISPLAY!r}
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=sys.argv[1], headless=False, args={list(LAUNCH_ARGS)!r},
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("about:blank")
        await context.close()

asyncio.run(main())
"""


def profile_version(profile: Path) -> str | None:
    """Chromium version that last opened ``profile`` (its "Last Version" file)."""
    try:
        return (profile / "Last Version").read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def patchright_version() -> str | None:
    """Chromium version patchright installs, from its driver's browsers.json."""
    try:
        spec = importlib.util.find_spec("patchright")
        if spec is None or not spec.submodule_search_locations:
            return None
        pkg = Path(next(iter(spec.submodule_search_locations)))
        data = json.loads((pkg / "driver" / "package" / "browsers.json").read_text())
        for entry in data.get("browsers", []):
            if entry.get("name") == "chromium":
                return str(entry["browserVersion"]) or None
    except (OSError, ValueError, KeyError, ImportError):
        return None
    return None


def backup_state() -> str:
    """ "ready" once the Chromium patchright installs has opened the profile.

    scripts/disk_hygiene.sh prunes Chromium profile backups only in that state.
    The versions must be EQUAL: a profile last opened by a newer Chromium (a
    downgrade) is one the installed Chromium cannot open, and its backup is
    the way back. The 14 days run from the backup's own mtime: the launch
    check opens the profile right after the copy, so that is the migration
    ("Last Version" is rewritten on every launch and dates nothing). The
    Camoufox engine's readiness says nothing about this.
    """
    last, new = profile_version(PROFILE_DIR), patchright_version()
    if last is None or new is None:
        return "unknown"
    return "ready" if last == new else "not_opened_by_the_new_chromium"


def _newer(a: str, b: str) -> bool:
    """Whether dotted version ``a`` is newer than ``b``; False when unreadable."""
    try:
        return tuple(map(int, a.split("."))) > tuple(map(int, b.split(".")))
    except ValueError:
        return False


def provision(
    run: Callable[..., subprocess.CompletedProcess],
    say: Callable[[str], None],
    backup: Callable[[], None],
    *,
    step_timeout: float,
    smoke_timeout: float,
) -> bool:
    """Install, back up the profile, and check the launch; True when it launches.

    ``backup`` raises (RuntimeError, which the transaction's ProvisionError is,
    or OSError) when the profile cannot be copied.
    """
    try:
        proc = run(
            [sys.executable, "-m", "patchright", "install", "chromium"], timeout=step_timeout
        )
    except subprocess.TimeoutExpired:
        say(f"FAILED: patchright install did not finish in {step_timeout}s")
        return False
    if proc.returncode != 0:
        say(f"FAILED: patchright install chromium exited {proc.returncode}")
        return False
    try:
        backup()
    except (RuntimeError, OSError) as exc:
        say(f"FAILED: Chromium profile backup: {exc}")
        return False
    last, new = profile_version(PROFILE_DIR), patchright_version()
    if last and new and _newer(last, new):
        # The check would open it with the older build; leave it as backed up.
        say(
            f"FAILED: the Chromium profile was last opened by newer Chromium {last} "
            f"than the installed {new}; not opening it (a copy is kept beside it)"
        )
        return False
    return _launches_or_deps(run, say, step_timeout, smoke_timeout)


# What a launch prints when the cure is `install-deps`: Playwright's own host
# check, and the dynamic loader. Anything else is not a missing package.
_MISSING_LIBS = ("Host system is missing dependencies", "error while loading shared libraries")


def _launch_failure(run, smoke_timeout: float) -> str | None:
    """None when the fallback's launch works, else the last lines it printed."""
    try:
        proc = run(
            [sys.executable, "-c", SMOKE, str(PROFILE_DIR)],
            capture_output=True,
            text=True,
            timeout=smoke_timeout,
        )
    except subprocess.TimeoutExpired:
        return f"did not start within {smoke_timeout}s"
    if proc.returncode == 0:
        return None
    return "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-3:])


def _launches_or_deps(run, say, step_timeout: float, smoke_timeout: float) -> bool:
    """`patchright install chromium` downloads the browser but not the Linux
    libraries it needs; Playwright's docs require `install-deps` for those. Run it
    only when the launch fails, and only through non-interactive sudo, which never
    prompts."""
    failure = _launch_failure(run, smoke_timeout)
    if failure is None:
        return True
    if not any(sign in failure for sign in _MISSING_LIBS):
        # A missing display, a locked or unreadable profile: no package fixes it.
        say(
            "FAILED: Chromium did not launch (the fallback runs it headed on the "
            f"X display {DISPLAY}: run scripts/setup-vnc.sh if that display is down)"
        )
        for line in failure.splitlines():
            say(f"    {line}")
        return False
    deps = [sys.executable, "-m", "patchright", "install-deps", "chromium"]
    try:
        sudo_ok = run(["sudo", "-n", "true"], capture_output=True).returncode == 0
    except FileNotFoundError:
        sudo_ok = False
    if sudo_ok:
        say("Chromium did not launch; installing its system libraries (install-deps)...")
        with contextlib.suppress(subprocess.TimeoutExpired):
            run(["sudo", "-n", *deps], timeout=step_timeout)
        failure = _launch_failure(run, smoke_timeout)
        if failure is None:
            return True
    say(
        "FAILED: Chromium did not launch; its system libraries may still be "
        f"missing (the X display {DISPLAY} too); run: sudo {' '.join(deps)}"
    )
    for line in failure.splitlines():
        say(f"    {line}")
    return False
