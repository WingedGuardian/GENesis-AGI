"""WS5 Discord capability SHADOW-gate — observe-only.

At each autonomous Discord egress door, record what a capability gate WOULD decide
(hold vs allow) WITHOUT holding. The lookup is READ-ONLY (no ``apply_event`` /
``ensure_cell`` — a not-yet-created cell simply reads as ``not_determined``), the
record is BEST-EFFORT (any failure — a ``None`` db, a missing table, a locked DB — is
swallowed at DEBUG), and the function NEVER raises, NEVER blocks, and NEVER mutates
capability state. The real send always proceeds. This is the observation phase before
the ENFORCE stage turns the gate on.

Coverage (the three live Discord doors — enumerated, exhaustive):
- ``deliver`` — ``outreach/pipeline._deliver`` (server process) -> discord webhook
- ``poll``    — ``mcp/outreach_mcp.outreach_poll`` (outreach subprocess) -> webhook
- ``reply``   — ``mcp/discord_bot_mcp.send_reply`` (discord-bot subprocess) -> API
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from genesis.autonomy.types import CellState
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import capability_shadow

logger = logging.getLogger(__name__)

_DOMAIN = "discord"
_PREVIEW_MAX = 200


async def observe_discord_send(
    db,
    *,
    path: str,
    verb: str,
    risk_class: str,
    target: str | None,
    content: str | None,
) -> bool:
    """Record a Discord-send capability-shadow observation. Returns True iff a row was
    written (False on a no-op/best-effort failure). Reads the (discord, verb,
    risk_class) cell READ-ONLY: a GRANTED cell => ``would_allow``; anything else (incl.
    a cell that doesn't exist yet) => ``would_hold``."""
    if db is None:
        return False
    try:
        from genesis.outreach.governance import content_hash

        cell = await cg.get_cell(db, _DOMAIN, verb, risk_class)
        # None => the cell has never been created => not_determined => would hold.
        state = cell["state"] if cell else None
        would_hold = state != CellState.GRANTED.value
        text = content or ""
        return await capability_shadow.record(
            db,
            id=str(uuid.uuid4()),
            observed_at=datetime.now(UTC).isoformat(),
            path=path,
            channel=_DOMAIN,
            cell_domain=_DOMAIN,
            cell_verb=verb,
            cell_risk_class=risk_class,
            cell_state=state,
            would_hold=would_hold,
            target=target,
            content_preview=text[:_PREVIEW_MAX],
            content_hash=content_hash(text),
        )
    except Exception:  # noqa: BLE001 — shadow is best-effort; NEVER break the real send
        logger.debug("capability shadow observe failed (best-effort)", exc_info=True)
        return False
