"""The free space a write can actually use on the filesystem holding a path.

statvfs alone overstates it on an install whose btrfs subvolume carries a quota:
the kernel refuses a write at the quota (EDQUOT) while the pool still shows room.
The disk guardian's ``dg_measure`` (scripts/lib/disk_guardian.sh) already takes the
tighter of statvfs and the quota, minus its outstanding reservations, so this asks
it rather than porting that logic. The library is found beside this checkout's
``src``, which an editable install (how Genesis runs) keeps in place; anywhere it
is missing or fails, statvfs answers instead and a warning says so, once.

``tier_after_write`` asks the guardian's own ``dg_floor_tier`` which tier the disk
is in now and would be in after a write, under the thresholds the watchdog runs
with: it sources scripts/tmp_watchgod.sh (which only defines functions when
sourced) and calls its ``load_config``, so the conf files and the reset of a
malformed value are the watchdog's own. ``effective_free_bytes`` is the
provisioner's free-space check (genesis.browser.provision).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from genesis.env import repo_root
from genesis.hostmetrics.readings import existing_parent

logger = logging.getLogger(__name__)

_GUARDIAN_LIB = repo_root() / "scripts" / "lib" / "disk_guardian.sh"
_WATCHGOD = repo_root() / "scripts" / "tmp_watchgod.sh"
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
            env=_guardian_env(),
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


_TIER_SCRIPT = r"""
# The answer goes to the descriptor the caller reads ($1's conf files run as shell
# code, and anything they print must not spoil it); everything else to stderr.
exec 4>&1 1>&2
. "$1" || exit 1
# The watchdog's own config loading: its conf files, and malformed values reset to
# its defaults. What it would log goes to stderr, for the caller to report as its own
# (through a copy of the descriptor: its log() discards the group's own stderr).
exec 3>&2
LOG_FILE=/dev/fd/3
load_config
# Whole megabytes, bounded well below where shell arithmetic wraps.
[[ "$3" =~ ^[0-9]{1,15}$ ]] || exit 1
m="$(dg_measure "$2")" || exit 1
read -r free total _quota unalloc meta _rest <<< "$m"
[[ "$free" =~ ^[0-9]+$ && "$total" =~ ^[0-9]+$ ]] || exit 1
after=$(( free - $3 ))
(( after < 0 )) && after=0
# On btrfs a write can allocate chunks from the unallocated space the metadata
# rule reads, so the after-tier assumes it does.
unalloc_after="$unalloc"
if [[ "$unalloc" =~ ^[0-9]+$ ]]; then
    unalloc_after=$(( unalloc - $3 ))
    (( unalloc_after < 0 )) && unalloc_after=0
fi
# The free-space tier only, not the time-to-full one: a write's own burst raises
# that one on nearly any yellow disk for a poll or two (owner ruling 2026-10-12).
now="$(dg_floor_tier "$free" "$total" "$unalloc" "$meta")"
later="$(dg_floor_tier "$after" "$total" "$unalloc_after" "$meta")"
echo "$now $later" >&4
"""
_TIERS = frozenset({"green", "yellow", "orange", "red"})
# The watchdog unit runs with none of these: a value inherited from whoever calls
# would move its lines (DG_*) or run code before it (BASH_ENV and the like).
_ENV_DROP = ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "POSIXLY_CORRECT")


def _guardian_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("DG_") and k not in _ENV_DROP}


def tier_after_write(path: Path | str, write_bytes: int) -> tuple[str, str] | None:
    """The disk guardian's free-space tier (green, yellow, orange or red) of the
    filesystem that would hold ``path``, now and once ``write_bytes`` more are
    written there. The watchdog's time-to-full is left out: a write's own burst
    raises it for a poll or two. None, with a warning, when the guardian cannot judge
    it: a tier has no statvfs stand-in. The script is the checkout's; a watchdog started before
    a deploy runs the copy it loaded, until it restarts."""
    probe = existing_parent(path)
    # Whole megabytes, a partial one counted as whole; an int, which the shell needs.
    write_mb = -(-max(int(write_bytes), 0) // 1024**2)
    try:
        proc = subprocess.run(
            [
                "bash",
                "-c",
                _TIER_SCRIPT,
                "dg",
                str(_WATCHGOD),
                str(probe),
                str(write_mb),
            ],
            capture_output=True,
            text=True,
            timeout=_MEASURE_TIMEOUT_S,
            check=False,
            env=_guardian_env(),
        )
        tiers = tuple(proc.stdout.split())
        if proc.returncode == 0 and len(tiers) == 2 and set(tiers) <= _TIERS:
            if proc.stderr.strip():  # the watchdog's config warnings, reported as ours
                logger.warning("disk guardian: %s", proc.stderr.strip()[:500])
            return tiers[0], tiers[1]
        reason = f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]!r}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = repr(exc)
    logger.warning("disk guardian could not tier %s (%s)", probe, reason)
    return None


def effective_free_bytes(path: Path | str) -> int | None:
    """Bytes free for a write under ``path``; see ``effective_usage``."""
    usage = effective_usage(path)
    return usage[0] if usage else None
