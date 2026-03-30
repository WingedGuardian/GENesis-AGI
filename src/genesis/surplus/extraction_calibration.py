"""Extraction calibration executor — weekly surplus task.

Groups extracted memories by confidence bucket and calculates retrieval
rates per bucket. Surfaces false-negative indicators (low-confidence
memories with high retrieval counts suggest threshold should be lower).

Results are stored as observations for the weekly self-assessment to
pick up.

Note: Memory data lives in Qdrant (payloads), not SQLite. The FTS5
table is a search index only. This module queries Qdrant via scroll
to collect confidence and retrieval stats.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)

# Confidence buckets for analysis
_BUCKETS = [
    ("very_low", 0.0, 0.3),
    ("low", 0.3, 0.5),
    ("medium", 0.5, 0.7),
    ("high", 0.7, 1.01),  # 1.01 to include 1.0
]


async def run_calibration(db: aiosqlite.Connection) -> dict:
    """Analyze extraction quality by confidence bucket.

    Queries Qdrant for memories with source='session_extraction',
    groups by confidence, and calculates retrieval rates.

    Returns a summary dict with per-bucket stats and overall health.
    """
    summary = {
        "buckets": {},
        "total_extracted": 0,
        "total_retrieved": 0,
        "false_negative_signal": False,
        "calibration_timestamp": datetime.now(UTC).isoformat(),
    }

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from genesis.env import qdrant_url

        qdrant = QdrantClient(url=qdrant_url(), timeout=10)

        # Scroll through all session_extraction memories
        all_points = []
        offset = None
        while True:
            results, next_offset = qdrant.scroll(
                collection_name="episodic_memory",
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="source",
                            match=MatchValue(value="session_extraction"),
                        ),
                    ],
                ),
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            all_points.extend(results)
            if next_offset is None or not results:
                break
            offset = next_offset

        if not all_points:
            logger.info("No extraction memories found for calibration")
            return summary

        # Bucket the points
        bucket_data: dict[str, list] = {name: [] for name, _, _ in _BUCKETS}

        for point in all_points:
            payload = point.payload or {}
            confidence = payload.get("confidence", 0.5)
            retrieved_count = payload.get("retrieved_count", 0)

            for bucket_name, low, high in _BUCKETS:
                if low <= confidence < high:
                    bucket_data[bucket_name].append({
                        "confidence": confidence,
                        "retrieved_count": retrieved_count,
                    })
                    break

        for bucket_name, _low, _high in _BUCKETS:
            points = bucket_data[bucket_name]
            total = len(points)
            retrieved = sum(1 for p in points if p["retrieved_count"] > 0)
            avg_retrievals = (
                sum(p["retrieved_count"] for p in points) / total if total > 0 else 0.0
            )
            avg_confidence = (
                sum(p["confidence"] for p in points) / total if total > 0 else 0.0
            )
            retrieval_rate = retrieved / total if total > 0 else 0.0

            summary["buckets"][bucket_name] = {
                "total": total,
                "retrieved": retrieved,
                "retrieval_rate": round(retrieval_rate, 3),
                "avg_retrievals": round(avg_retrievals, 2),
                "avg_confidence": round(avg_confidence, 3),
            }
            summary["total_extracted"] += total
            summary["total_retrieved"] += retrieved

            # False negative signal: low-confidence memories with high
            # retrieval counts suggest the confidence scoring is too conservative
            if bucket_name in ("very_low", "low") and avg_retrievals > 2.0 and total > 5:
                summary["false_negative_signal"] = True

    except Exception:
        logger.error("Calibration Qdrant query failed", exc_info=True)
        return summary

    # Store as observation if we have meaningful data
    if summary["total_extracted"] > 0:
        try:
            overall_rate = summary["total_retrieved"] / summary["total_extracted"]
            observation_content = (
                f"Extraction calibration: {summary['total_extracted']} memories, "
                f"{summary['total_retrieved']} retrieved ({overall_rate:.1%}). "
            )

            if summary["false_negative_signal"]:
                observation_content += (
                    "FALSE NEGATIVE SIGNAL: low-confidence memories have high "
                    "retrieval rates — consider lowering confidence thresholds."
                )

            for name, bucket in summary["buckets"].items():
                if isinstance(bucket, dict) and "total" in bucket:
                    observation_content += (
                        f"\n  {name}: {bucket['total']} memories, "
                        f"{bucket['retrieval_rate']:.1%} retrieved"
                    )

            from genesis.db.crud import observations as obs_crud

            await obs_crud.create(
                db,
                content=observation_content,
                source="extraction_calibration",
                obs_type="calibration_result",
                priority="low",
            )
            logger.info("Calibration observation stored: %s", observation_content[:120])

        except Exception:
            logger.error("Failed to store calibration observation", exc_info=True)

    return summary
