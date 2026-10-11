"""In-container Claude holds SH from pre-launch reads through exec return, not child; mutations hold EX via scripts/lib/checkout_lock.sh."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_POLL_S = 0.25
# ASSUMED, unmeasured: a healthy exclusive hold lasts seconds
LONG_WAIT_WARN_S = 600.0
_WARNED_FAIL_OPEN = False
_LOCK_PATH_CACHE: dict[str, Path] = {}


def _lock_path_for(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    common_dir = result.stdout.strip()
    if result.returncode != 0 or not common_dir:
        return None
    return Path(common_dir).resolve() / "genesis-checkout.lock"


def checkout_lock_path() -> Path | None:
    from genesis.env import repo_root

    try:
        root = str(Path(repo_root()).resolve())
    except Exception:
        return None
    cached = _LOCK_PATH_CACHE.get(root)
    if cached is not None:
        return cached
    path = _lock_path_for(Path(root))
    if path is not None:
        _LOCK_PATH_CACHE[root] = path
    return path


def _warn_fail_open(reason: str) -> None:
    global _WARNED_FAIL_OPEN
    if not _WARNED_FAIL_OPEN:
        _WARNED_FAIL_OPEN = True
        logger.warning("checkout launch admission is fail-open: %s", reason)


class CheckoutAdmission:
    def __init__(self, fd: int | None = None) -> None:
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            os.close(fd)


async def admit_launch() -> CheckoutAdmission:
    try:
        path = await asyncio.to_thread(checkout_lock_path)
    except Exception as exc:
        _warn_fail_open(f"cannot resolve lock path: {exc}")
        return CheckoutAdmission()
    if path is None:
        _warn_fail_open("repository has no resolvable Git common directory")
        return CheckoutAdmission()
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        _warn_fail_open(f"cannot open {path}: {exc}")
        return CheckoutAdmission()

    started = time.monotonic()
    warned = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return CheckoutAdmission(fd)
            except BlockingIOError:
                elapsed = time.monotonic() - started
                if not warned and elapsed >= LONG_WAIT_WARN_S:
                    warned = True
                    logger.error(
                        "checkout mutation has held %s for %.0f seconds; waiting for launch admission",
                        path,
                        elapsed,
                    )
                await asyncio.sleep(_POLL_S)
    except BaseException:
        os.close(fd)
        raise


@asynccontextmanager
async def checkout_admission() -> AsyncIterator[CheckoutAdmission]:
    admission = await admit_launch()
    try:
        yield admission
    finally:
        admission.release()
