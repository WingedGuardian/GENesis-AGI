"""board_promote — propose promoting a private record onto the work board.

The MCP door to :mod:`genesis.board.promotion`. Any session may PROPOSE; the
owner approves each promotion, and only then does the drain post the public
issue. Nothing reaches GitHub from this call.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _db_or_none():
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    return _service._db if _service is not None else None


@mcp.tool()
async def board_promote(
    source: str,
    title: str,
    body: str,
    acceptance_criteria: list[str] | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Propose turning a private ledger row or follow-up into a PUBLIC GitHub
    issue on the work board. Held for the owner's approval; nothing is posted
    by this call.

    ``source`` is ``ledger:<id>`` or ``follow_up:<id>`` (full id or a unique
    8+ hex prefix). ``title`` and ``body`` are what the PUBLIC issue will say —
    write them for a public reader: technical detail only, nothing personal or
    install-specific. ``acceptance_criteria`` become a checklist. The draft is
    privacy-scanned here; a refusal names only the line number (title = line 1)
    and the scanner, so rewrite that line.

    Refused when the board mode is off, when an unverified open question blocks
    the source (resolve it first), or when the source is already promoted or has
    a promotion pending. The same checks run again right before the issue is
    created: an approved promotion whose record has since closed, or whose
    label is gone, is ended unposted (propose it again once fixed). Returns
    ``held`` / ``disabled`` / ``refused`` / ``blocked`` / ``duplicate`` /
    ``error``.
    """
    from genesis.board import promotion

    db = _db_or_none()
    if db is None:
        return {"status": "unavailable", "message": "DB not initialized"}
    return await promotion.propose(
        db,
        source=source,
        title=title,
        body=body,
        acceptance_criteria=acceptance_criteria,
        labels=labels,
    )
