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
    """(ActiveState, UnitFileState) in ONE `systemctl show`, or (None, None).

    Both properties come from a single spawn: `show -p A -p B --value` returns
    them newline-separated in request order. Two spawns would double this
    collector's worst-case contribution to every refresh on every install —
    and the unit file is rendered everywhere, so this path is always taken.
    """
    if shutil.which("systemctl") is None:
        return (None, None)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "show", _UNIT,
            "-p", "ActiveState", "-p", "UnitFileState", "--value",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
    except TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        logger.warning("infra_profile: systemctl show %s timed out", _UNIT)
        return (None, None)
    except Exception:
        return (None, None)
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
