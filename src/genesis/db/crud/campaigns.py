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
    session_profile: str = "research",
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
    only_if_pending: bool = False,
) -> int:
    """Mark a run as completed with outcome details.

    When ``only_if_pending`` is True the UPDATE is gated on the row still
    being ``outcome='pending'`` — used as an optimistic lock so two concurrent
    capture paths (the cron tick and the pending-session reaper) cannot both
    "win" and double-process the same run. Returns the number of rows changed
    (1 if this caller claimed the run, 0 if it was already completed).
    """
    sql = (
        "UPDATE campaign_runs "
        "SET outcome = ?, summary = ?, skip_reason = ?, cost_usd = ?, "
        "    session_id = ?, finished_at = ? "
        "WHERE id = ?"
    )
    params: list[Any] = [
        outcome, summary, skip_reason, cost_usd, session_id, finished_at, run_id,
    ]
    if only_if_pending:
        sql += " AND outcome = 'pending'"
    cursor = await db.execute(sql, params)
    await db.commit()
    return cursor.rowcount


async def increment_campaign_totals(
    db: Any, campaign_id: str, cost_usd: float
) -> None:
    """Atomically bump a campaign's run/cost counters.

    Uses a SQL-level increment (``total_runs = total_runs + 1``) rather than a
    read-modify-write so concurrent capture paths cannot clobber each other's
    update and lose a count.
    """
    await db.execute(
        "UPDATE campaigns "
        "SET total_runs = total_runs + 1, total_cost_usd = total_cost_usd + ? "
        "WHERE id = ?",
        (cost_usd, campaign_id),
    )
    await db.commit()


async def get_run(db: Any, run_id: str) -> dict | None:
    """Fetch a single campaign run by ID."""
    cursor = await db.execute(
        "SELECT * FROM campaign_runs WHERE id = ?", (run_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def mark_orphan_runs_superseded(
    db: Any,
    campaign_id: str,
    keep_run_id: str | None,
    older_than: str,
    finished_at: str,
) -> int:
    """Resolve abandoned pending runs left by a crash/double-dispatch.

    A run is orphaned when it is still ``outcome='pending'`` but is no longer
    the campaign's active pending run (``id != keep_run_id``) — its dispatch
    was superseded without ever being captured. Marks such rows
    ``outcome='error', skip_reason='superseded'`` so attempt/run accounting
    reconciles. ``keep_run_id`` of None resolves all (other) pending rows.

    ``older_than`` (ISO timestamp) is a grace guard: only rows whose
    ``started_at < older_than`` are eligible. This protects a run that is in
    the middle of being dispatched (created, but its state pointer not yet
    written) from being mistaken for an orphan. Returns rows reconciled.
    """
    sql = (
        "UPDATE campaign_runs "
        "SET outcome = 'error', skip_reason = 'superseded', finished_at = ? "
        "WHERE campaign_id = ? AND outcome = 'pending' AND started_at < ?"
    )
    params: list[Any] = [finished_at, campaign_id, older_than]
    if keep_run_id is not None:
        sql += " AND id != ?"
        params.append(keep_run_id)
    cursor = await db.execute(sql, params)
    await db.commit()
    return cursor.rowcount


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


async def count_runs_by_outcome(db: Any, campaign_id: str) -> dict[str, int]:
    """Return run counts grouped by outcome (pending/success/skip/error).

    Lets the dashboard show honest "completed vs attempts" numbers rather than
    conflating dispatched-but-uncaptured runs with completed ones.
    """
    cursor = await db.execute(
        "SELECT outcome, COUNT(*) AS n FROM campaign_runs "
        "WHERE campaign_id = ? GROUP BY outcome",
        (campaign_id,),
    )
    return {row["outcome"]: int(row["n"]) for row in await cursor.fetchall()}


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
