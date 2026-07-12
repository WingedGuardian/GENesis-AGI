"""immunity_status MCP tool — WS-3 B1 injection-gate SHADOW observability.

The B1 deliverable, made readable: per-gate live mode + per-site would-block
counts from ``immunity_shadow_events``, so the operator (and the eventual B4
enforce decision) can see HOW MUCH external content reaches each action-capable
inject site, and WHERE — the data that sizes the enforce blast radius.

Read-only and **gate-agnostic**: it iterates the canonical
:data:`genesis.security.immunity.GATES` for live modes and merges whatever gate
rows exist in the shadow store, so when gates 1-3 (procedure / identity /
autonomy) begin writing their own would-blocks they surface here with NO code
change. Reuses :func:`genesis.security.immunity_shadow.recent_summary` (its
self-resolving connection); no new SQL, no new schema, nothing blocked.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp
from genesis.security import immunity, immunity_shadow

logger = logging.getLogger(__name__)


def _shape(
    *,
    summary_rows: list[dict],
    gate_modes: dict[str, str],
    master_enabled: bool,
    since: str | None,
) -> dict:
    """Pure shaping: fold per-(gate, site) count rows into a per-gate view.

    The gate set is the UNION of the canonical modes and any gate present in the
    rows — an unexpected gate is surfaced with mode ``"unknown"`` rather than
    silently dropped (a schema/emit drift must be visible). Only ``would_block``
    rows count toward the totals.
    """
    gates: dict[str, dict] = {}
    for name in set(gate_modes) | {r["gate"] for r in summary_rows}:
        gates[name] = {
            "mode": gate_modes.get(name, "unknown"),
            "would_block_total": 0,
            "by_site": [],
        }

    for r in summary_rows:
        if not r.get("would_block"):
            continue
        entry = gates[r["gate"]]
        n = int(r.get("n", 0))
        entry["would_block_total"] += n
        entry["by_site"].append({"source_ref": r.get("source_ref"), "count": n})

    for entry in gates.values():
        entry["by_site"].sort(key=lambda s: s["count"], reverse=True)

    return {
        "status": "ok",
        "master_enabled": master_enabled,
        "window": since or "all-time",
        "gates": gates,
        "note": (
            "WS-3 immunity gates — SHADOW observability. would_block_total = "
            "recall events where an external-untrusted item WOULD have been "
            "blocked at that inject site (nothing is actually blocked in shadow); "
            "owner/first-party never produce a row. Gate-agnostic: gates 1-3 "
            "surface here once they emit. Read-only."
        ),
    }


async def _impl_immunity_status(since: str | None = None) -> dict:
    """Read live gate modes + shadow would-block counts. Best-effort read-only."""
    try:
        master_enabled = bool(immunity.load_immunity_config().get("enabled", True))
        gate_modes = {gate: immunity.gate_mode(gate) for gate in immunity.GATES}
        summary_rows = await immunity_shadow.recent_summary(since=since)
        return _shape(
            summary_rows=summary_rows,
            gate_modes=gate_modes,
            master_enabled=master_enabled,
            since=since,
        )
    except Exception:
        logger.debug("immunity_status read failed", exc_info=True)
        return {"status": "unavailable", "message": "immunity shadow read failed"}


@mcp.tool()
async def immunity_status(since: str | None = None) -> dict:
    """How much external content reaches each action-capable inject site — the
    WS-3 immunity SHADOW readout.

    Per-gate live mode (off / shadow / enforce) plus per-site would-block counts
    from the immunity shadow log. In shadow NOTHING is blocked; these counts size
    the enforce (B4) blast radius. Optionally bound to rows at/after ISO ``since``.
    Read-only; does NOT change behaviour.
    """
    return await _impl_immunity_status(since=since)
