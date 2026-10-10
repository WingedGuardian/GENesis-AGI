"""The deploy marker, claimed the way the shell deploys claim it (#2525).

``~/.genesis/update_in_progress.pid`` holds the bare pid of whatever is
deploying; ``genesis.env.update_in_progress()`` reads it, and the watchdog defers
restarting the server while that pid is alive. Every shell writer
(scripts/lib/deploy_marker.sh, used by deploy_code_only.sh, deploy_candidates and
restore.sh) checks and writes it only while holding ``locks/update.lock``, and
update.sh holds that lock for its whole run. The dashboard's update routes use
these helpers so their check-then-write cannot interleave with a deploy's.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
from collections.abc import Iterator
from pathlib import Path

from genesis.env import _marker_holder_live

logger = logging.getLogger(__name__)


class UpdateLockError(OSError):
    """update.lock could not be opened at all (not merely held by someone)."""


@contextlib.contextmanager
def holding_update_lock(marker: Path) -> Iterator[bool]:
    """Yield True while this thread holds update.lock exclusively, False when
    another process holds it. Raises UpdateLockError when the lock cannot be
    opened, so a permission error never reads as "a deploy is running".

    Never blocks. A fresh open() per call, because flock conflicts per open
    file, so two request threads exclude each other; the fd is close-on-exec,
    so no child inherits the lock. The lock lives beside the marker, as in the
    shell scripts.
    """
    path = marker.parent / "locks" / "update.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a")  # noqa: SIM115 - closed below, which releases the lock
    except OSError as exc:
        raise UpdateLockError(f"cannot open {path}: {exc}") from exc
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()


def marker_pid(marker: Path) -> int | None:
    try:
        return int(marker.read_text().strip())
    except (OSError, ValueError):
        return None


def clear_dead_marker(marker: Path) -> None:
    """Remove the marker unless a live process holds it. Call under the lock."""
    pid = marker_pid(marker)
    if pid is not None and pid > 1 and _marker_holder_live(pid):
        return
    marker.unlink(missing_ok=True)


def release_marker(marker: Path, pid: int) -> None:
    """Remove the marker only while it still holds ``pid``. A deploy holding the
    lock may have just replaced a dead holder's pid with its own, so a busy or
    unopenable lock leaves the file alone: a dead pid there reads as "no
    deploy" anyway."""
    try:
        with holding_update_lock(marker) as held:
            if held and marker_pid(marker) == pid:
                marker.unlink(missing_ok=True)
    except UpdateLockError:
        logger.warning("could not release the deploy marker", exc_info=True)
