"""The Chromium fallback's step of the browser-stack provisioning transaction.

genesis.browser.provision runs :func:`provision` after the Camoufox engine is in
place and launches. The step is non-fatal (the Chromium fallback is not the
primary layer), so it never rolls the browser packages back:

  1. ``patchright install chromium``;
  2. copy the Chromium profile once before a newer Chromium major first opens it
     (the caller's ``backup`` does the copy);
  3. launch the same Chromium binary the fallback runs. When it does not start,
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

# The fallback launches Chromium HEADED (mcp/health/browser.py), which Playwright
# runs as the full ``chromium`` binary; a plain headless launch runs
# ``chromium-headless-shell`` instead, which links fewer system libraries, so it
# can pass where the fallback cannot start. ``channel="chromium"`` makes a headless launch use the
# full binary (playwright's chromium.js getExecutableName).
SMOKE = """
import asyncio
from patchright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chromium", headless=True)
        page = await browser.new_page()
        await page.goto("about:blank")
        await browser.close()

asyncio.run(main())
"""


def profile_major(profile: Path) -> int | None:
    """Major Chromium version that last opened ``profile`` (its "Last Version" file)."""
    try:
        return int((profile / "Last Version").read_text().strip().split(".", 1)[0])
    except (OSError, ValueError):
        return None


def patchright_major() -> int | None:
    """Chromium major patchright installs, from its driver's browsers.json."""
    try:
        spec = importlib.util.find_spec("patchright")
        if spec is None or not spec.submodule_search_locations:
            return None
        pkg = Path(next(iter(spec.submodule_search_locations)))
        data = json.loads((pkg / "driver" / "package" / "browsers.json").read_text())
        for entry in data.get("browsers", []):
            if entry.get("name") == "chromium":
                return int(str(entry["browserVersion"]).split(".", 1)[0])
    except (OSError, ValueError, KeyError, ImportError):
        return None
    return None


def backup_state() -> str:
    """ "ready" once the Chromium patchright installs has opened the profile.

    scripts/disk_hygiene.sh prunes Chromium profile backups only in that state:
    the profile then records a Chromium at least as new as patchright's, so the
    upgraded browser has run with it. The Camoufox engine's readiness says
    nothing about the Chromium stack.
    """
    last, new = profile_major(PROFILE_DIR), patchright_major()
    if last is None or new is None:
        return "unknown"
    return "ready" if last >= new else "not_opened_by_the_new_chromium"


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
    return _launches_or_deps(run, say, step_timeout, smoke_timeout)


def _launches(run, smoke_timeout: float) -> bool:
    try:
        proc = run(
            [sys.executable, "-c", SMOKE], capture_output=True, text=True, timeout=smoke_timeout
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _launches_or_deps(run, say, step_timeout: float, smoke_timeout: float) -> bool:
    """`patchright install chromium` downloads the browser but not the Linux
    libraries it needs; Playwright's docs require `install-deps` for those. Run it
    only when the launch fails, and only through non-interactive sudo, which never
    prompts."""
    if _launches(run, smoke_timeout):
        return True
    deps = [sys.executable, "-m", "patchright", "install-deps", "chromium"]
    try:
        sudo_ok = run(["sudo", "-n", "true"], capture_output=True).returncode == 0
    except FileNotFoundError:
        sudo_ok = False
    if sudo_ok:
        say("Chromium did not launch; installing its system libraries (install-deps)...")
        with contextlib.suppress(subprocess.TimeoutExpired):
            run(["sudo", "-n", *deps], timeout=step_timeout)
        if _launches(run, smoke_timeout):
            return True
    say(
        "FAILED: Chromium did not launch; its system libraries may be missing. "
        f"Run: sudo {' '.join(deps)}"
    )
    return False
