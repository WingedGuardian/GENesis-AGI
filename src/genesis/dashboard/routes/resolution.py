"""Error resolution routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint


@blueprint.route("/api/genesis/errors/<path:group_key>/resolve", methods=["POST"])
@_async_route
async def resolve_error(group_key: str):
    """Manually resolve an error group."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    data = request.get_json(silent=True) or {}
    now = datetime.now(UTC).isoformat()
    await rt.db.execute(
        """INSERT INTO resolved_errors (id, error_group_key, resolved_by, resolved_at, notes)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(error_group_key) DO UPDATE SET
             resolved_by = excluded.resolved_by,
             resolved_at = excluded.resolved_at,
             notes = excluded.notes""",
        (str(uuid.uuid4()), group_key, data.get("resolved_by", "user"), now, data.get("notes", "")),
    )
    await rt.db.commit()
    return jsonify({"ok": True, "resolved_at": now})


@blueprint.route("/api/genesis/errors/<path:group_key>/resolve", methods=["DELETE"])
@_async_route
async def unresolve_error(group_key: str):
    """Remove manual resolution for an error group."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    await rt.db.execute("DELETE FROM resolved_errors WHERE error_group_key = ?", (group_key,))
    await rt.db.commit()
    return jsonify({"ok": True})
