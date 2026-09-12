"""Step 2.3 — Route learning signals from delta attributions to write targets."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from genesis.db.crud import capability_gaps
from genesis.learning.types import DiscoveryAttribution, OutcomeClass, RequestDeliveryDelta

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import aiosqlite


class _ObservationWriter(Protocol):
    async def write(
        self,
        db: Any,
        *,
        source: str,
        type: str,
        content: str,
        priority: str,
        category: str | None = None,
        origin_class: str | None = None,
    ) -> str: ...


async def route_learning_signals(
    db: aiosqlite.Connection,
    delta: RequestDeliveryDelta,
    outcome: OutcomeClass,
    observation_writer: _ObservationWriter,
    *,
    origin_class: str | None = None,
) -> dict[str, str]:
    """Route each attribution to its concrete write target.

    ``origin_class`` (WS-3): the analyzed session's channel origin, forwarded to
    every ``retrospective`` observation write. ``delta.evidence`` characterizes
    the analyzed conversation, so an external session's evidence must not be
    stamped first-party. See :func:`genesis.cc.types.observation_origin_for_channel`.

    Returns summary of actions taken.
    """
    actions: dict[str, str] = {}

    for attr in delta.attributions:
        if attr == DiscoveryAttribution.EXTERNAL_LIMITATION:
            await observation_writer.write(
                db,
                source="retrospective",
                type="external_limitation",
                content=delta.evidence,
                priority="medium",
                origin_class=origin_class,
            )
            actions["external_limitation"] = "observation_written"

        elif attr == DiscoveryAttribution.USER_MODEL_GAP:
            await observation_writer.write(
                db,
                source="retrospective",
                type="user_model_gap",
                content=delta.evidence,
                priority="high",
                origin_class=origin_class,
            )
            actions["user_model_gap"] = "observation_written"

        elif attr == DiscoveryAttribution.GENESIS_CAPABILITY:
            if outcome == OutcomeClass.CAPABILITY_GAP:
                now = datetime.now(UTC).isoformat()
                det_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, delta.evidence))
                await capability_gaps.upsert(
                    db,
                    id=det_id,
                    description=delta.evidence,
                    gap_type="capability_gap",
                    first_seen=now,
                    last_seen=now,
                )
                actions["genesis_capability"] = "capability_gap_recorded"
            else:
                await observation_writer.write(
                    db,
                    source="retrospective",
                    type="capability_improvement",
                    content=delta.evidence,
                    priority="medium",
                    origin_class=origin_class,
                )
                actions["genesis_capability"] = "observation_written"

        elif attr == DiscoveryAttribution.GENESIS_INTERPRETATION:
            await observation_writer.write(
                db,
                source="retrospective",
                type="interpretation_correction",
                content=delta.evidence,
                priority="medium",
                origin_class=origin_class,
            )
            actions["genesis_interpretation"] = "observation_written"

        elif attr == DiscoveryAttribution.SCOPE_UNDERSPECIFIED:
            await observation_writer.write(
                db,
                source="retrospective",
                type="scope_clarification",
                content=delta.evidence,
                priority="low",
                origin_class=origin_class,
            )
            actions["scope_underspecified"] = "observation_written"

        elif attr == DiscoveryAttribution.USER_REVISED_SCOPE:
            actions["user_revised_scope"] = "tracked"

    # Create speculative claims for low-confidence patterns (hypothesis for later validation)
    if delta.evidence and outcome not in (OutcomeClass.SUCCESS, OutcomeClass.CLASSIFICATION_FAILED):
        try:
            from datetime import timedelta

            from genesis.db.crud import speculative

            hypothesis = f"{outcome.value}: {delta.evidence[:500]}"
            now = datetime.now(UTC)
            claim_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, hypothesis))
            await speculative.upsert(
                db,
                id=claim_id,
                claim=hypothesis,
                hypothesis_expiry=(now + timedelta(days=30)).isoformat(),
                created_at=now.isoformat(),
            )
            actions["speculative_claim"] = "created"
        except Exception:
            logger.debug("Speculative claim creation failed", exc_info=True)

    return actions
