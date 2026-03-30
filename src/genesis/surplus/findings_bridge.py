"""Bridge code audit findings to surplus_insights staging table.

Findings are staged for Genesis proper (via reflection) to review and
promote/discard. They do NOT go to the observations table directly —
free-model output must pass through Genesis proper before influencing
reasoning or reaching the user.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from genesis.db.crud import surplus

logger = logging.getLogger(__name__)

# Minimum confidence to stage a finding. Below this threshold, findings are
# likely generic/speculative and not worth persisting.
_MIN_CONFIDENCE = 0.7

_TTL_DAYS = 7


class FindingsBridge:
    """Writes code audit findings to surplus_insights for Genesis proper to review.

    Findings are staged with promotion_status='pending'. Deep reflection
    reads surplus.list_pending() and decides whether to promote or discard.
    """

    def __init__(self, db) -> None:
        self._db = db

    async def bridge_findings(self, insights: list[dict]) -> int:
        """Write findings to surplus_insights table. Returns count of new findings written."""
        written = 0
        for insight in insights:
            try:
                written += await self._bridge_one(insight)
            except Exception:
                logger.error(
                    "Failed to bridge finding: %s",
                    insight,
                    exc_info=True,
                )
        return written

    async def _bridge_one(self, insight: dict) -> int:
        # Confidence gate — skip low-confidence / generic findings
        try:
            confidence = float(insight.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < _MIN_CONFIDENCE:
            logger.debug(
                "Low-confidence finding skipped (%.2f): %s",
                confidence, str(insight.get("suggestion", ""))[:80],
            )
            return 0

        # Hash only semantically meaningful fields for dedup
        hash_keys = {
            k: insight.get(k) for k in ("file", "line", "severity", "suggestion")
        }
        content_hash = hashlib.sha256(
            json.dumps(hash_keys, sort_keys=True).encode()
        ).hexdigest()[:16]

        # Dedup: use content_hash as id prefix, check with exact prefix match
        id_prefix = f"audit-{content_hash}"
        cursor = await self._db.execute(
            "SELECT 1 FROM surplus_insights WHERE id = ? OR id GLOB ?",
            (id_prefix, f"{id_prefix}-*"),
        )
        if await cursor.fetchone():
            logger.debug("Duplicate finding skipped: %s", content_hash)
            return 0

        now = datetime.now(UTC)
        ttl = (now + timedelta(days=_TTL_DAYS)).isoformat()

        await surplus.create(
            self._db,
            id=f"{id_prefix}-{uuid.uuid4().hex[:8]}",
            content=json.dumps(insight),
            source_task_type="code_audit",
            generating_model=insight.get("model", "unknown"),
            drive_alignment="competence",
            confidence=confidence,
            created_at=now.isoformat(),
            ttl=ttl,
        )

        return 1
