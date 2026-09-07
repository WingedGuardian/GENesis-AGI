"""FalkorDB graph-engine facts — provisioning state, not query behaviour.

The engine (issue #1641) is a derived, rebuildable index over ``memory_links``;
SQLite stays the system of record. This collector reports whether the SERVER
side is provisioned and running. It deliberately does NOT connect to the engine
or issue a query: the client is not a dependency at this stage, and a
collector that needed one would fail on every install that has not adopted the
engine yet.

``collect_systemd`` already enumerates ``genesis-*`` units, so the unit's
enablement and state are covered there — that is exactly why the unit is named
``genesis-falkordb.service``. What is unique here is the pairing: the module
artifact on disk, the socket the unit is supposed to create, and whether those
agree with the unit's state. A unit that is active while its socket is missing
is the one combination that means something is actually wrong.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from genesis.infra_profile.collectors._probe import (
    ProbeFailed,
    reap,
    user_bus_present,
)
from genesis.infra_profile.types import SectionResult

logger = logging.getLogger(__name__)

_UNIT = "genesis-falkordb.service"
_CMD_TIMEOUT = 5.0


def _paths() -> tuple[Path, Path, Path]:
    """(deps root, data dir, rendered unit) — resolved from $HOME, as the unit is."""
    home = Path.home()
    return (
        home / ".genesis" / "deps" / "falkordb",
        home / ".genesis" / "falkordb",
        home / ".config" / "systemd" / "user" / _UNIT,
    )


def _installed_versions(deps_root: Path) -> list[str]:
    """Version dirs that actually contain a module, newest-sorting last.

    A directory without ``falkordb.so`` is an interrupted or refused install
    (a checksum mismatch cleans up after itself, but a killed download between
    mkdir and fetch would leave the dir), so presence of the FILE is what
    counts as installed — not presence of the directory.
    """
    if not deps_root.is_dir():
        return []
    found = []
    try:
        for child in deps_root.iterdir():
            if child.is_dir() and (child / "falkordb.so").is_file():
                found.append(child.name)
    except OSError:
        return []
    return sorted(found)


async def _unit_states() -> tuple[str | None, str | None]:
    """(ActiveState, UnitFileState) in ONE `systemctl show`.

    Returns (None, None) ONLY when there is no systemctl to ask: a box without
    systemd has no unit state, and that is an answer rather than a failure.
    A systemctl that EXISTS and then fails RAISES instead, so
    `collect_falkordb` degrades the whole section.

    That routing is load-bearing, not tidiness. `unit_enabled` is a HASHED
    fact, and `service._merge_section` keeps the prior facts and hash for a
    non-ok section — its own comment calls that "no phantom drift". Answering
    None on a transient failure would instead flip a hashed fact, billing a
    drift observation plus an LLM annotation regeneration on the way out and
    again on the way back, for a hiccup that changed no configuration at all.
    It would also let the posture rule read an all-clear off a state nobody
    could actually verify.

    Both properties come from a single spawn: `show -p A -p B --value` returns
    them newline-separated in request order. Two spawns would double this
    collector's worst-case contribution to every refresh on every install —
    and the unit file is rendered everywhere, so this path is always taken.
    """
    if shutil.which("systemctl") is None:
        return (None, None)

    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "show", _UNIT,
        "-p", "ActiveState", "-p", "UnitFileState", "--value",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
    except TimeoutError:
        await reap(proc)
        logger.warning("infra_profile: systemctl show %s timed out", _UNIT)
        raise ProbeFailed(
            f"systemctl show {_UNIT} timed out after {_CMD_TIMEOUT}s"
        ) from None
    except BaseException:
        # BaseException deliberately: a CANCELLED refresh still leaves a child,
        # and CancelledError is not an Exception.
        await reap(proc)
        raise

    if proc.returncode != 0:
        # `systemctl show` exits 0 even for a unit that does not exist
        # (measured), so a nonzero exit can never mean "absent" — the question
        # could not be asked at all.
        #
        # Split by WHY, the same way `shutil.which` above splits absent from
        # failed. A box with no user manager has no bus to reach, will not
        # grow one on the next refresh, and answering "no unit state" for it
        # is a fact. Erroring the section instead would be permanent, and
        # would discard four facts we DID read off the filesystem
        # (unit_present, module_installed, module_versions, socket_path)
        # because one sub-probe of the same section was unanswerable.
        if not user_bus_present():
            return (None, None)
        logger.warning(
            "infra_profile: systemctl show %s exited %s", _UNIT, proc.returncode
        )
        raise ProbeFailed(f"systemctl show {_UNIT} exited {proc.returncode}")

    lines = stdout.decode(errors="replace").splitlines()
    active = lines[0].strip() if len(lines) > 0 else ""
    enabled = lines[1].strip() if len(lines) > 1 else ""
    return (active or None, enabled or None)


async def collect_falkordb() -> SectionResult:
    """Provisioning state of the graph engine. Never raises; degrades to error."""
    try:
        deps_root, data_dir, unit_path = _paths()
        versions = await asyncio.to_thread(_installed_versions, deps_root)
        socket_path = data_dir / "falkordb.sock"
        socket_present = await asyncio.to_thread(socket_path.exists)
        unit_present = await asyncio.to_thread(unit_path.is_file)

        active, enabled = await _unit_states() if unit_present else (None, None)

        # The facts/metrics split follows types.py's contract literally, because
        # facts are HASHED and a hash change costs a drift observation plus an
        # LLM annotation regeneration. `unit_active_state` flips on every
        # restart and `socket_present` follows it, so both are volatile —
        # exactly the "states" the contract names on the metrics side. Putting
        # them in facts would bill a model call for every engine restart.
        #
        # `unit_enabled` stays a FACT: it changes only when someone deliberately
        # runs enable/disable, which is slow-changing configuration.
        facts = {
            "unit": _UNIT,
            "unit_present": unit_present,
            "unit_enabled": enabled,
            "module_versions": versions,
            "module_installed": bool(versions),
            "socket_path": str(socket_path),
        }
        metrics = {
            "unit_active_state": active,
            "socket_present": socket_present,
        }
        return SectionResult(name="falkordb", facts=facts, metrics=metrics)
    except Exception as exc:  # a collector must never take down the refresh
        return SectionResult.failed("falkordb", f"falkordb facts unavailable: {exc}")
