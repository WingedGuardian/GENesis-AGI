"""WS5 capability SHADOW-gate — observe-only (Discord doors today).

At each autonomous external egress door, record what a capability gate WOULD decide
(hold vs allow) WITHOUT holding. The lookup is READ-ONLY (no ``apply_event`` /
``ensure_cell`` — a not-yet-created cell simply reads as ``not_determined``), the
record is BEST-EFFORT (any failure — a ``None`` db, a missing table, a locked DB — is
swallowed at DEBUG), and the function NEVER raises, NEVER blocks, and NEVER mutates
capability state. The real send always proceeds. This is the observation phase before
the ENFORCE stage turns the gate on.

EGRESS-GATING CONTRACT — read before wiring ANY new outbound channel:
- **Owner-facing channels are NEVER gated.** Delivery TO the owner — Telegram-to-owner,
  voice/TTS, email-to-owner — is not external-world posting (the owner IS the recipient);
  gating it only obstructs the user. There is no gate on these today; keep it that way.
- **Every autonomous EXTERNAL/public egress MUST be gated** — shadow-observed here now,
  enforced later. Live external egress today: email (ENFORCES via ``autonomy.email_gate``)
  and Discord (SHADOW-observed at the 3 doors below). Any FUTURE public channel — Medium,
  Twitter/X, Slack, autonomous ``distribution.DistributionManager.distribute`` publishing —
  MUST route its send through this shadow-gate before the enforce stage. Do not ship an
  unobserved external door.
- **CI backstop:** ``scripts/check_external_io.py`` fails the build on a new Discord/webhook/
  public-social endpoint outside its allowlist and documents the same owner-vs-external rule
  (its lines 24-27). It CANNOT see browser/Composio egress or a ``distribute()`` call with no
  literal endpoint token — those carry a ``# GROUNDWORK(autonomous-distribution)`` marker at
  the call sites (``distribution/manager.py``, ``modules/content_pipeline/module.py``) instead.

Coverage (the three live Discord doors — enumerated, exhaustive):
- ``deliver`` — ``outreach/pipeline._deliver`` (server process) -> discord webhook
- ``poll``    — ``mcp/outreach_mcp.outreach_poll`` (outreach subprocess) -> webhook
- ``reply``   — ``mcp/discord_bot_mcp.send_reply`` (discord-bot subprocess) -> API
"""

from __future__ import annotations

import hashlib
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
            # Hash the FULL content — a genuine fingerprint for dedup/analysis, distinct
            # from the bounded content_preview (first N chars). NOT governance.content_hash,
            # which truncates to 200 chars (would collide with the preview span).
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )
    except Exception:  # noqa: BLE001 — shadow is best-effort; NEVER break the real send
        logger.debug("capability shadow observe failed (best-effort)", exc_info=True)
        return False
