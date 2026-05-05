"""CRUD operations for autonomy_state table."""

from __future__ import annotations

import aiosqlite

# ── Bayesian level computation ───────────────────────────────────────────────
# Beta distribution posterior mean with Laplace smoothing:
#   posterior = (successes + 1) / (successes + corrections + 2)
# Same math as procedural Laplace smoothing in operations.py.

_LEVEL_THRESHOLDS = (
    (0.70, 4),  # posterior >= 0.70 → L4
    (0.50, 3),  # posterior >= 0.50 → L3
    (0.30, 2),  # posterior >= 0.30 → L2
)


def bayesian_level(total_successes: int, total_corrections: int) -> int:
    """Compute autonomy level from Beta distribution posterior mean.

    With zero interactions, returns L1 (no evidence to promote).
    Examples:
      50S + 2C → posterior 0.94 → L4 (stable at high performance)
       3S + 2C → posterior 0.57 → L3 (drops under mixed evidence)
       0S + 2C → posterior 0.25 → L1 (low confidence)
    """
    total = total_successes + total_corrections
    if total == 0:
        return 1  # No evidence yet
    posterior = (total_successes + 1) / (total + 2)
    for threshold, level in _LEVEL_THRESHOLDS:
        if posterior >= threshold:
            return level
    return 1


def bayesian_posterior(total_successes: int, total_corrections: int) -> float:
    """Return the posterior mean for observability/logging."""
    total = total_successes + total_corrections
    if total == 0:
        return 0.5  # Uninformative prior
    return (total_successes + 1) / (total + 2)


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    category: str,
    updated_at: str,
    person_id: str | None = None,
    current_level: int = 1,
    earned_level: int = 1,
    context_ceiling: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO autonomy_state
           (id, person_id, category, current_level, earned_level, context_ceiling, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (id, person_id, category, current_level, earned_level, context_ceiling, updated_at),
    )
    await db.commit()
    return id


async def upsert(
    db: aiosqlite.Connection,
    *,
    id: str,
    category: str,
    updated_at: str,
    person_id: str | None = None,
    current_level: int = 1,
    earned_level: int = 1,
    context_ceiling: str | None = None,
) -> str:
    """Idempotent write: insert or update on conflict."""
    await db.execute(
        """INSERT INTO autonomy_state
           (id, person_id, category, current_level, earned_level, context_ceiling, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             person_id = excluded.person_id,
             category = excluded.category, current_level = excluded.current_level,
             earned_level = excluded.earned_level, context_ceiling = excluded.context_ceiling,
             updated_at = excluded.updated_at""",
        (id, person_id, category, current_level, earned_level, context_ceiling, updated_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute("SELECT * FROM autonomy_state WHERE id = ?", (id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_category(
    db: aiosqlite.Connection,
    category: str,
    *,
    person_id: str | None = None,
) -> dict | None:
    sql = "SELECT * FROM autonomy_state WHERE category = ?"
    params: list = [category]
    if person_id is not None:
        sql += " AND person_id = ?"
        params.append(person_id)
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_all(
    db: aiosqlite.Connection, *, person_id: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM autonomy_state"
    params: list = []
    if person_id is not None:
        sql += " WHERE person_id = ?"
        params.append(person_id)
    sql += " ORDER BY category"
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def record_correction(
    db: aiosqlite.Connection, id: str, *, corrected_at: str, updated_at: str
) -> bool:
    """Record a correction. Bayesian regression: recompute posterior, drop level if warranted."""
    row = await get_by_id(db, id)
    if not row:
        return False
    new_consecutive = row["consecutive_corrections"] + 1
    new_corrections = row["total_corrections"] + 1

    # Bayesian regression — level derived from posterior, not hard-coded counter
    target = bayesian_level(row["total_successes"], new_corrections)
    new_level = min(row["current_level"], target)  # can only stay or drop

    regression_reason = row["regression_reason"]
    last_regression = row["last_regression_at"]
    if new_level < row["current_level"]:
        new_consecutive = 0
        posterior = bayesian_posterior(row["total_successes"], new_corrections)
        regression_reason = f"Bayesian regression (posterior={posterior:.3f}) at {corrected_at}"
        last_regression = corrected_at
    cursor = await db.execute(
        """UPDATE autonomy_state SET
           consecutive_corrections = ?, total_corrections = ?,
           current_level = ?, last_correction_at = ?,
           regression_reason = ?, last_regression_at = ?, updated_at = ?
           WHERE id = ?""",
        (new_consecutive, new_corrections, new_level, corrected_at,
         regression_reason, last_regression, updated_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def record_success(
    db: aiosqlite.Connection, id: str, *, updated_at: str
) -> bool:
    """Record success. Increments counter only — promotion requires explicit approve."""
    row = await get_by_id(db, id)
    if not row:
        return False
    new_successes = row["total_successes"] + 1

    cursor = await db.execute(
        """UPDATE autonomy_state SET
           total_successes = ?, consecutive_corrections = 0,
           updated_at = ?
           WHERE id = ?""",
        (new_successes, updated_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def promote(
    db: aiosqlite.Connection, id: str, *, to_level: int, updated_at: str
) -> bool:
    """Explicit promotion — only called on user approval."""
    row = await get_by_id(db, id)
    if not row or to_level < 1 or to_level > 7:
        return False
    if to_level <= row["current_level"]:
        return False  # Not a promotion
    new_earned = max(row["earned_level"], to_level)
    cursor = await db.execute(
        """UPDATE autonomy_state SET
           current_level = ?, earned_level = ?, updated_at = ?
           WHERE id = ?""",
        (to_level, new_earned, updated_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def force_regress(
    db: aiosqlite.Connection,
    id: str,
    *,
    to_level: int = 1,
    reason: str = "user_revoked",
    updated_at: str,
) -> bool:
    """Hard reset — resets BOTH current and earned level. For user revocation."""
    row = await get_by_id(db, id)
    if not row or to_level < 1 or to_level > 7:
        return False
    cursor = await db.execute(
        """UPDATE autonomy_state SET
           current_level = ?, earned_level = ?,
           regression_reason = ?, last_regression_at = ?, updated_at = ?
           WHERE id = ?""",
        (to_level, to_level, reason, updated_at, updated_at, id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute("DELETE FROM autonomy_state WHERE id = ?", (id,))
    await db.commit()
    return cursor.rowcount > 0
