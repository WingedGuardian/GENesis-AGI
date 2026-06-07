"""CRUD operations for campaigns and campaign_runs tables."""

from __future__ import annotations

from typing import Any


async def create_campaign(
    db: Any,
    *,
    id: str,
    name: str,
    strategy_doc_path: str,
    cron_cadence: str,
    created_at: str,
    model: str = "sonnet",
    effort: str = "medium",
    session_profile: str = "interact",
    status: str = "active",
    state_json: str = "{}",
    pre_checks: str = '["rate_limit", "budget", "slots_available"]',
    max_daily_cost_usd: float = 1.0,
) -> str:
    """Insert a new campaign. Returns the campaign ID."""
    await db.execute(
        """INSERT INTO campaigns
           (id, name, strategy_doc_path, cron_cadence, model, effort,
            session_profile, status, state_json, pre_checks,
            max_daily_cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            id, name, strategy_doc_path, cron_cadence, model, effort,
            session_profile, status, state_json, pre_checks,
            max_daily_cost_usd, created_at,
        ),
    )
    await db.commit()
    return id


async def get_campaign_by_name(db: Any, name: str) -> dict | None:
    """Fetch a campaign by its unique name slug."""
    cursor = await db.execute(
        "SELECT * FROM campaigns WHERE name = ?", (name,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_campaign(db: Any, campaign_id: str) -> dict | None:
    """Fetch a campaign by ID."""
    cursor = await db.execute(
        "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_campaigns(
    db: Any,
    status_filter: str | None = None,
) -> list[dict]:
    """List campaigns, optionally filtered by status."""
    if status_filter:
        cursor = await db.execute(
            "SELECT * FROM campaigns WHERE status = ? ORDER BY created_at DESC",
            (status_filter,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        )
    return [dict(r) for r in await cursor.fetchall()]


async def update_campaign(db: Any, campaign_id: str, **fields: Any) -> None:
    """Update arbitrary fields on a campaign row."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [campaign_id]
    await db.execute(
        f"UPDATE campaigns SET {set_clause} WHERE id = ?",  # noqa: S608
        values,
    )
    await db.commit()


async def update_campaign_state(
    db: Any, campaign_id: str, state_json: str
) -> None:
    """Update the campaign's state JSON blob."""
    await db.execute(
        "UPDATE campaigns SET state_json = ? WHERE id = ?",
        (state_json, campaign_id),
    )
    await db.commit()


async def create_run(
    db: Any,
    *,
    id: str,
    campaign_id: str,
    started_at: str,
    trigger_type: str = "scheduled",
    state_snapshot: str | None = None,
) -> str:
    """Insert a new campaign run. Returns the run ID."""
    await db.execute(
        """INSERT INTO campaign_runs
           (id, campaign_id, started_at, trigger_type, state_snapshot)
           VALUES (?, ?, ?, ?, ?)""",
        (id, campaign_id, started_at, trigger_type, state_snapshot),
    )
    await db.commit()
    return id


async def complete_run(
    db: Any,
    run_id: str,
    *,
    outcome: str,
    summary: str | None = None,
    skip_reason: str | None = None,
    cost_usd: float = 0.0,
    session_id: str | None = None,
    finished_at: str | None = None,
) -> None:
    """Mark a run as completed with outcome details."""
    await db.execute(
        """UPDATE campaign_runs
           SET outcome = ?, summary = ?, skip_reason = ?, cost_usd = ?,
               session_id = ?, finished_at = ?
           WHERE id = ?""",
        (outcome, summary, skip_reason, cost_usd, session_id, finished_at, run_id),
    )
    await db.commit()


async def list_runs(
    db: Any,
    campaign_id: str,
    limit: int = 10,
) -> list[dict]:
    """List recent runs for a campaign, newest first."""
    cursor = await db.execute(
        """SELECT * FROM campaign_runs
           WHERE campaign_id = ?
           ORDER BY started_at DESC
           LIMIT ?""",
        (campaign_id, limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_daily_cost(
    db: Any,
    campaign_id: str,
    date_str: str,
) -> float:
    """Sum cost_usd for runs on a given date (YYYY-MM-DD prefix match)."""
    cursor = await db.execute(
        """SELECT COALESCE(SUM(cost_usd), 0.0) AS total
           FROM campaign_runs
           WHERE campaign_id = ?
             AND started_at LIKE ? || '%'""",
        (campaign_id, date_str),
    )
    row = await cursor.fetchone()
    return float(row["total"]) if row else 0.0
