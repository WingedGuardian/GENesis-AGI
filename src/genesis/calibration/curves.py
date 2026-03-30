"""Calibration curve computation — pure programmatic, no LLM."""

from __future__ import annotations

import logging
from collections import defaultdict

import aiosqlite

from genesis.db.crud import predictions as pred_crud

logger = logging.getLogger(__name__)


class CalibrationCurveComputer:
    """Computes calibration correction curves per domain x confidence bucket."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def compute(self, domain: str) -> list[dict]:
        matched = await pred_crud.get_matched_by_domain(self._db, domain)
        if not matched:
            return []

        buckets: dict[str, list[bool]] = defaultdict(list)
        for row in matched:
            buckets[row["confidence_bucket"]].append(bool(row["correct"]))

        curves = []
        for bucket, outcomes in sorted(buckets.items()):
            sample_count = len(outcomes)
            actual_rate = sum(outcomes) / sample_count
            try:
                low, high = bucket.split("-")
                predicted = (float(low) + float(high)) / 2
            except (ValueError, IndexError):
                predicted = 0.5
            correction = actual_rate / predicted if predicted > 0 else 1.0
            curves.append({
                "confidence_bucket": bucket,
                "predicted_confidence": predicted,
                "actual_success_rate": actual_rate,
                "sample_count": sample_count,
                "correction_factor": correction,
            })
        return curves

    async def compute_and_save(self, domain: str) -> list[dict]:
        curves = await self.compute(domain)
        for curve in curves:
            await pred_crud.save_calibration_curve(
                self._db,
                domain=domain,
                **curve,
            )
        if curves:
            logger.info("Saved %d calibration curves for domain=%s", len(curves), domain)
        return curves
