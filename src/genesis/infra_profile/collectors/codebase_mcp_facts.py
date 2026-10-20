"""codebase-memory-mcp setup facts — registered, configured, enabled, opted out.

The facts and their verdict live in ``genesis.infra_profile.codebase_mcp``,
which the installers' notice (``scripts/lib/cbm_setup_notice.sh``) also runs,
so the posture alert and the install/update line cannot disagree. This file
only adds what a collector owes the profile: the user-manager split (a box with
no user manager has no unit state, and that is an answer) and the degrade-to-
error contract (a probe that EXISTS and fails retains the previous facts
instead of hashing a guess).

Everything here is slow-changing configuration (an operator ran configure, ran
enable, created the sentinel), so it is all facts, none of it metrics.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from genesis.env import repo_root
from genesis.infra_profile import codebase_mcp
from genesis.infra_profile.types import SectionResult
from genesis.util.systemd import systemctl_env

logger = logging.getLogger(__name__)


def _user_manager_present() -> bool:
    """Is there a user manager the managed status probe can reach?

    The probe runs with the shared normalized environment (status uses
    systemctl_env), so this asks about the SAME runtime directory — a server
    started without XDG_RUNTIME_DIR still has a manager at the default one.
    """
    runtime = Path(systemctl_env()["XDG_RUNTIME_DIR"])
    return (runtime / "systemd" / "private").is_socket() or (runtime / "bus").is_socket()


def _read() -> dict:
    return codebase_mcp.read_facts(repo_root(), Path.home(), strict_manager=_user_manager_present())


async def collect_codebase_mcp() -> SectionResult:
    """Never raises; an unanswerable probe degrades the section to error.

    So does a registered server whose opt-out path cannot be resolved: that
    verdict is "could not tell", and an ok section carrying it would RESOLVE an
    open alert while nothing has been fixed. As an error, the refresh retains
    the previous facts and the posture check holds its all-clear. (No user
    manager is the one "could not tell" that stays ok: it is a standing answer
    about this box, which strict_manager has already separated out.)
    """
    try:
        facts = await asyncio.to_thread(_read)
    except Exception as exc:  # a collector must never take down the refresh
        logger.warning("codebase_mcp facts unavailable", exc_info=True)
        return SectionResult.failed("codebase_mcp", f"codebase-memory-mcp facts unavailable: {exc}")
    if facts.get("registered") is True and facts.get("disabled") is None:
        return SectionResult.failed(
            "codebase_mcp",
            "codebase-memory-mcp opt-out path could not be resolved "
            "(CODEBASE_MEMORY_MCP_DISABLE_FILE must be an absolute path)",
        )
    return SectionResult(name="codebase_mcp", facts=facts)
