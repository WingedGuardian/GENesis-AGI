"""Budget configuration routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import aiosqlite
from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/budgets", methods=["GET"])
@_async_route
async def get_budgets():
    """Return active budget limits."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify([])

    rt.db.row_factory = aiosqlite.Row
    cursor = await rt.db.execute(
        "SELECT budget_type, limit_usd, warning_pct FROM budgets WHERE active = 1"
    )
    rows = await cursor.fetchall()
    return jsonify([dict(r) for r in rows])


@blueprint.route("/api/genesis/budgets", methods=["POST"])
@_async_route
async def set_budget():
    """Create or update a budget limit."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    budget_type = data.get("budget_type")
    limit_usd = data.get("limit_usd")
    warning_pct = max(0.01, min(1.0, float(data.get("warning_pct", 0.80))))

    if budget_type not in ("daily", "weekly", "monthly"):
        return jsonify({"error": "budget_type must be daily/weekly/monthly"}), 400
    if not isinstance(limit_usd, (int, float)) or limit_usd <= 0:
        return jsonify({"error": "limit_usd must be a positive number"}), 400

    now = datetime.now(UTC).isoformat()
    await rt.db.execute(
        "UPDATE budgets SET active = 0 WHERE budget_type = ? AND active = 1",
        (budget_type,),
    )
    await rt.db.execute(
        """INSERT INTO budgets (id, budget_type, limit_usd, warning_pct, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?)""",
        (str(uuid.uuid4()), budget_type, limit_usd, warning_pct, now, now),
    )
    await rt.db.commit()
    return jsonify({"ok": True, "budget_type": budget_type, "limit_usd": limit_usd})
