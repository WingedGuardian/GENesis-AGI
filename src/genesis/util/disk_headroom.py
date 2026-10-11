"""The free space a write can actually use on the filesystem holding a path.

statvfs alone overstates it on an install whose btrfs subvolume carries a quota:
the kernel refuses a write at the quota (EDQUOT) while the pool still shows room.
The disk guardian's ``dg_measure`` (scripts/lib/disk_guardian.sh) already takes the
tighter of statvfs and the quota, minus its outstanding reservations, so this asks
it rather than porting that logic. The library is found beside this checkout's
``src``, which an editable install (how Genesis runs) keeps in place; anywhere it
is missing or fails, statvfs answers instead and a warning says so, once.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from genesis.hostmetrics.readings import existing_parent

logger = logging.getLogger(__name__)

_GUARDIAN_LIB = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "disk_guardian.sh"
# Reading a mount table and a few sysfs files; nothing in it waits on the network.
_MEASURE_TIMEOUT_S = 60
_warned = False


def effective_usage(path: Path | str) -> tuple[int, int] | None:
    """``(free, total)`` bytes for the filesystem that would hold ``path`` (which may
    not exist yet: measured on its nearest existing parent), as the disk guardian
    sees them: when a quota binds, both are the quota's. None when not even statvfs
    can answer."""
    global _warned
    probe = existing_parent(path)
    try:
        proc = subprocess.run(
            ["bash", "-c", '. "$1" && dg_measure "$2"', "dg", str(_GUARDIAN_LIB), str(probe)],
            capture_output=True,
            text=True,
            timeout=_MEASURE_TIMEOUT_S,
            check=False,
        )
        fields = proc.stdout.split()
        if (
            proc.returncode == 0
            and len(fields) >= 2
            and fields[0].isdigit()
            and fields[1].isdigit()
        ):
            return int(fields[0]) * 1024**2, int(fields[1]) * 1024**2
        reason = f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]!r}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = repr(exc)
    (logger.debug if _warned else logger.warning)(
        "disk guardian could not measure %s (%s); using statvfs, which ignores quotas",
        probe,
        reason,
    )
    _warned = True
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    return usage.free, usage.total


def effective_free_bytes(path: Path | str) -> int | None:
    """Bytes free for a write under ``path``; see ``effective_usage``."""
    usage = effective_usage(path)
    return usage[0] if usage else None
