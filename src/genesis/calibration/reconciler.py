"""Prediction-outcome reconciliation — matches predictions to actual results."""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import predictions as pred_crud

logger = logging.getLogger(__name__)


class PredictionReconciler:
    """Batch job: matches unmatched predictions to observed outcomes."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def reconcile_outreach(self) -> int:
        unmatched = await pred_crud.list_unmatched(self._db, domain="outreach")
        count = 0
        for pred in unmatched:
            action_id = pred["action_id"]
            cursor = await self._db.execute(
                "SELECT engagement_outcome FROM outreach_history "
                "WHERE id = ? AND engagement_outcome IS NOT NULL",
                (action_id,),
            )
            row = await cursor.fetchone()
            if not row:
                continue
            outcome = row[0] if isinstance(row, tuple) else row["engagement_outcome"]
            correct = outcome == "engaged"
            await pred_crud.record_outcome(
                self._db, pred["id"], outcome=outcome, correct=correct,
            )
            count += 1
        if count:
            logger.info("Reconciled %d outreach predictions", count)
        return count

    async def reconcile_all(self) -> dict[str, int]:
        results = {}
        results["outreach"] = await self.reconcile_outreach()
        return results
