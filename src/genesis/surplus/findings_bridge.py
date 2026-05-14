"""Bridge code audit findings to the intelligence intake pipeline.

Findings are routed through intake (atomize → score → knowledge base).
Falls back to surplus_insights staging if the intake pipeline is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging

from genesis.db.crud import surplus

logger = logging.getLogger(__name__)

# Minimum confidence to stage a finding. Below this threshold, findings are
# likely generic/speculative and not worth persisting.
_MIN_CONFIDENCE = 0.7


class FindingsBridge:
    """Routes code audit findings through the intake pipeline.

    Findings that pass the confidence gate are routed to the knowledge base
    via intake. If intake fails, falls back to surplus_insights staging.
    """

    def __init__(self, db) -> None:
        self._db = db

    async def bridge_findings(self, insights: list[dict]) -> int:
        """Route findings through intake. Returns count of findings processed."""
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

        # Hash for dedup check
        hash_keys = {
            k: insight.get(k) for k in ("file", "line", "severity", "suggestion")
        }
        content_hash = hashlib.sha256(
            json.dumps(hash_keys, sort_keys=True).encode()
        ).hexdigest()[:16]

        # Dedup: check surplus_insights for already-processed findings
        id_prefix = f"audit-{content_hash}"
        cursor = await self._db.execute(
            "SELECT 1 FROM surplus_insights WHERE id = ? OR id GLOB ?",
            (id_prefix, f"{id_prefix}-*"),
        )
        if await cursor.fetchone():
            logger.debug("Duplicate finding skipped: %s", content_hash)
            return 0

        # Route through intake pipeline
        content = json.dumps(insight)
        try:
            from genesis.surplus.intake import IntakeSource, run_intake
            await run_intake(
                content=content,
                source=IntakeSource.ANTICIPATORY_RESEARCH,
                source_task_type="code_audit",
                generating_model=insight.get("model", "unknown"),
                db=self._db,
            )
        except Exception:
            # Fallback: write to surplus_insights staging (old behavior)
            logger.warning("Intake failed for code audit finding — falling back to staging", exc_info=True)
            import uuid
            from datetime import UTC, datetime, timedelta
            now = datetime.now(UTC)
            ttl = (now + timedelta(days=7)).isoformat()
            await surplus.create(
                self._db,
                id=f"{id_prefix}-{uuid.uuid4().hex[:8]}",
                content=content,
                source_task_type="code_audit",
                generating_model=insight.get("model", "unknown"),
                drive_alignment="competence",
                confidence=confidence,
                created_at=now.isoformat(),
                ttl=ttl,
            )

        return 1
