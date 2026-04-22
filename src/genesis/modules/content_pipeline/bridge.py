"""Content→Outreach bridge — delivers content drafts to Telegram for review.

Connects the content pipeline's PublishManager to the outreach pipeline.
Content is routed to the "Content Review" Telegram forum topic via the
CONTENT outreach category. The user reviews and approves before external
publishing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.modules.content_pipeline.types import PublishResult
    from genesis.outreach.pipeline import OutreachPipeline

logger = logging.getLogger(__name__)


async def deliver_for_review(
    publish_result: PublishResult,
    outreach_pipeline: OutreachPipeline,
) -> str | None:
    """Submit a content draft to outreach for user review via Telegram.

    Uses the full outreach governance path (quiet hours, rate limits,
    dedup) — NOT submit_raw(). Content review respects the same
    delivery rules as other outreach categories.

    Returns the outreach_id on success, None on governance rejection.
    """
    from genesis.outreach.types import OutreachCategory, OutreachRequest, OutreachStatus

    platform = publish_result.platform
    content_preview = publish_result.content_text[:80].replace("\n", " ")

    request = OutreachRequest(
        category=OutreachCategory.CONTENT,
        topic=f"Content draft ({platform}): {content_preview}",
        context=publish_result.content_text,
        salience_score=0.8,  # Content review should almost always deliver
        signal_type="content_review",
        channel="telegram",
    )

    try:
        result = await outreach_pipeline.submit(request)
        if result.status == OutreachStatus.REJECTED:
            logger.info(
                "Content review rejected by governance: %s (publish_id=%s)",
                result.governance_result.reason if result.governance_result else "unknown",
                publish_result.id,
            )
            return None
        logger.info(
            "Content draft delivered for review: publish_id=%s outreach_id=%s",
            publish_result.id,
            result.outreach_id,
        )
        return result.outreach_id
    except Exception:
        logger.error(
            "Failed to deliver content for review: publish_id=%s",
            publish_result.id,
            exc_info=True,
        )
        return None
