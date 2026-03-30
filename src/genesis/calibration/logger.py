"""PredictionLogger — convenience wrapper for structured prediction logging."""

from __future__ import annotations

import logging
import uuid

import aiosqlite

from genesis.calibration.types import bucket_confidence
from genesis.db.crud import predictions as pred_crud

logger = logging.getLogger(__name__)


class PredictionLogger:
    """Logs structured predictions for Bayesian calibration."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def log(
        self,
        *,
        action_id: str,
        prediction: str,
        confidence: float,
        domain: str,
        reasoning: str,
    ) -> str:
        pred_id = str(uuid.uuid4())
        confidence = max(0.0, min(1.0, confidence))
        bucket = bucket_confidence(confidence)
        await pred_crud.log_prediction(
            self._db,
            id=pred_id,
            action_id=action_id,
            prediction=prediction,
            confidence=confidence,
            confidence_bucket=bucket,
            domain=domain,
            reasoning=reasoning,
        )
        logger.debug("Logged prediction %s [%s] conf=%.2f bucket=%s",
                      pred_id, domain, confidence, bucket)
        return pred_id
