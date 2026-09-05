"""Boot identity — which boot of this machine is currently running.

STDLIB-ONLY, deliberately. This is imported by the host-side Guardian, whose
venv carries ``pyyaml`` and nothing else (``scripts/install_guardian.sh``
installs exactly that). Anything under ``genesis.observability`` is off-limits
here: importing any of its submodules executes that package's ``__init__``,
which pulls ``aiohttp`` and ``aiosqlite`` — absent on the host, so the import
would raise and take the Guardian's whole tick down with it. The same constraint
is recorded at ``genesis/guardian/repo_bundle.py``.

``boot_id`` rather than ``/proc/stat``'s ``btime``: boot_id is a per-boot random
UUID, regenerated only on an actual boot and independent of the clock. ``btime``
is derived as wall-clock minus uptime, so it moves by the full amount of any NTP
step — and a step during a live incident would read as a reboot and discard the
episode, the exact false positive this mechanism must avoid. Comparing boot ids
needs no tolerance, no drift anchor, and no direction reasoning.
"""

from __future__ import annotations

# Module-level so tests can repoint it at a fake tree, mirroring the `_PROC`
# seam in genesis/observability/cc_slots.py.
_PROC = "/proc"


def read_boot_id() -> str | None:
    """This boot's UUID from ``/proc/sys/kernel/random/boot_id``, or None.

    None means "cannot tell" — never "rebooted". Callers must fail OPEN on it:
    treating an unreadable boot id as a reboot would silently discard live state.
    """
    try:
        with open(f"{_PROC}/sys/kernel/random/boot_id") as f:
            return f.read().strip() or None
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError from a repointed _PROC in tests;
        # the real file is ASCII. Either way the contract is "cannot tell".
        return None
