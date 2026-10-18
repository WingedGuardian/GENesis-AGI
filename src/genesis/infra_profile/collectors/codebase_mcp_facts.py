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
from pathlib import Path

from genesis.env import repo_root
from genesis.infra_profile import codebase_mcp
from genesis.infra_profile.collectors._probe import user_manager_present
from genesis.infra_profile.types import SectionResult


def _unit_state() -> str | None:
    try:
        return codebase_mcp.unit_file_state()
    except codebase_mcp.Unanswerable:
        if not user_manager_present():
            return None
        raise


async def collect_codebase_mcp() -> SectionResult:
    """Never raises; an unanswerable probe degrades the section to error."""
    try:
        facts = await asyncio.to_thread(
            codebase_mcp.read_facts, repo_root(), Path.home(), unit_state=_unit_state
        )
        return SectionResult(name="codebase_mcp", facts=facts)
    except Exception as exc:  # a collector must never take down the refresh
        return SectionResult.failed("codebase_mcp", f"codebase-memory-mcp facts unavailable: {exc}")
