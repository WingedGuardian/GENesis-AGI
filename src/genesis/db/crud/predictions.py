"""CRUD for predictions and calibration_curves tables."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


async def log_prediction(
    db: aiosqlite.Connection,
    *,
    id: str,
    action_id: str,
    prediction: str,
    confidence: float,
    confidence_bucket: str,
    domain: str,
    reasoning: str,
) -> None:
    await db.execute(
        "INSERT INTO predictions (id, action_id, prediction, confidence, "
        "confidence_bucket, domain, reasoning) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, action_id, prediction, confidence, confidence_bucket, domain, reasoning),
    )
    await db.commit()


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM predictions WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def record_outcome(
    db: aiosqlite.Connection, id: str, *, outcome: str, correct: bool,
) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE predictions SET outcome = ?, correct = ?, matched_at = ? WHERE id = ?",
        (outcome, int(correct), now, id),
    )
    await db.commit()


async def list_unmatched(
    db: aiosqlite.Connection, *, domain: str | None = None, limit: int = 100,
) -> list[dict]:
    query = "SELECT * FROM predictions WHERE outcome IS NULL"
    params: list = []
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    cursor = await db.execute(query, params)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in await cursor.fetchall()]


async def get_matched_by_domain(
    db: aiosqlite.Connection, domain: str,
) -> list[dict]:
    cursor = await db.execute(
        "SELECT confidence_bucket, correct FROM predictions "
        "WHERE domain = ? AND outcome IS NOT NULL",
        (domain,),
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in await cursor.fetchall()]


async def save_calibration_curve(
    db: aiosqlite.Connection,
    *,
    domain: str,
    confidence_bucket: str,
    predicted_confidence: float,
    actual_success_rate: float,
    sample_count: int,
    correction_factor: float,
) -> None:
    await db.execute(
        "INSERT INTO calibration_curves (domain, confidence_bucket, predicted_confidence, "
        "actual_success_rate, sample_count, correction_factor) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(domain, confidence_bucket) DO UPDATE SET "
        "predicted_confidence = excluded.predicted_confidence, "
        "actual_success_rate = excluded.actual_success_rate, "
        "sample_count = excluded.sample_count, "
        "correction_factor = excluded.correction_factor, "
        "computed_at = datetime('now')",
        (domain, confidence_bucket, predicted_confidence,
         actual_success_rate, sample_count, correction_factor),
    )
    await db.commit()


async def get_calibration_curves(
    db: aiosqlite.Connection, domain: str,
) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM calibration_curves WHERE domain = ? ORDER BY confidence_bucket",
        (domain,),
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in await cursor.fetchall()]
