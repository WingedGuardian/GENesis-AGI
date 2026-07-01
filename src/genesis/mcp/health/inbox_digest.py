"""inbox_digest MCP tool — prioritized digest of recent inbox evaluations.

Provides an on-demand summary of recent inbox evaluation output and
related follow-ups for foreground session review.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _get_db():
    """Late-import DB from the health MCP module state."""
    import genesis.mcp.health_mcp as health_mcp_mod

    svc = health_mcp_mod._service
    if svc is None:
        return None
    return getattr(svc, "_db", None)


# ---------------------------------------------------------------------------
# Implementation (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_inbox_digest(
    days: int = 7,
    include_resolved: bool = False,
) -> dict:
    """Build a prioritized digest of recent inbox evaluations and follow-ups.

    Returns a dict with:
    - summary: one-line status string
    - pending_follow_ups: list of inbox-sourced pending follow-ups
    - resolved_follow_ups: list of inbox-sourced completed follow-ups (if requested)
    - recent_evaluations: list of completed inbox items
    - formatted: markdown table for direct display
    """
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}

    try:
        from genesis.db.crud import follow_ups as fu_crud
        from genesis.db.crud import inbox_items as inbox_crud

        # 1. Pending inbox follow-ups
        pending = await fu_crud.get_by_source(
            db, "inbox_evaluation", status="pending",
        )

        # 2. Recently resolved inbox follow-ups
        resolved = []
        if include_resolved:
            resolved = await fu_crud.get_recently_resolved(
                db, source="inbox_evaluation", days=days,
            )

        # 3. Recent completed evaluations
        evals = await inbox_crud.get_recent_completed(db, days=days)

        # Build formatted output
        formatted = _format_digest(pending, resolved, evals, days)

        # Summary line
        parts = [f"{len(pending)} pending"]
        if resolved:
            parts.append(f"{len(resolved)} resolved")
        parts.append(f"{len(evals)} evaluated")
        summary = f"Inbox digest ({days}d): {', '.join(parts)}"

        return {
            "summary": summary,
            "pending_follow_ups": pending,
            "resolved_follow_ups": resolved,
            "recent_evaluations": evals,
            "formatted": formatted,
        }
    except Exception as exc:
        logger.error("inbox_digest failed", exc_info=True)
        return {"error": f"Failed to generate inbox digest: {exc}"}


def _format_digest(
    pending: list[dict],
    resolved: list[dict],
    evals: list[dict],
    days: int,
) -> str:
    """Format digest data into markdown tables."""
    lines = [f"## Inbox Digest — Last {days} Days", ""]

    # Pending action items
    if pending:
        lines.append(f"### Pending Action ({len(pending)} items)")
        lines.append("")
        lines.append("| Priority | Item | Strategy |")
        lines.append("|----------|------|----------|")
        for fu in pending:
            priority = fu.get("priority", "medium")
            content = fu.get("content", "")[:120]
            strategy = fu.get("strategy", "")
            lines.append(f"| {priority} | {content} | {strategy} |")
        lines.append("")

    # Resolved items
    if resolved:
        lines.append(f"### Recently Resolved ({len(resolved)} items)")
        lines.append("")
        lines.append("| Item | Resolution | When |")
        lines.append("|------|------------|------|")
        for fu in resolved:
            content = fu.get("content", "")[:100]
            notes = (fu.get("resolution_notes") or "—")[:80]
            completed_at = fu.get("completed_at", "")
            age = _relative_age(completed_at)
            lines.append(f"| {content} | {notes} | {age} |")
        lines.append("")

    # Recent evaluations
    if evals:
        lines.append(f"### Evaluations ({len(evals)} completed)")
        lines.append("")
        lines.append("| Date | Source | Response |")
        lines.append("|------|--------|----------|")
        for ev in evals:
            # Show the COMPLETION date (processed_at) — get_recent_completed
            # windows/orders by processed_at, and a reused row's created_at is
            # its (recent) re-arm time but a long-parked row's created_at is its
            # old detection time, so processed_at is the accurate "evaluated on"
            # date. Fall back to created_at if processed_at is somehow absent.
            date = (ev.get("processed_at") or ev.get("created_at") or "")[:10]
            source_file = Path(ev.get("file_path", "")).name
            response = Path(ev.get("response_path", "")).name if ev.get("response_path") else "—"
            lines.append(f"| {date} | {source_file} | {response} |")
        lines.append("")

    if not pending and not resolved and not evals:
        lines.append("No inbox activity in this period.")
        lines.append("")

    return "\n".join(lines)


def _relative_age(iso_timestamp: str) -> str:
    """Convert ISO timestamp to human-readable relative age."""
    if not iso_timestamp:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - ts
        if delta < timedelta(hours=1):
            return f"{max(1, int(delta.total_seconds() / 60))}m ago"
        if delta < timedelta(days=1):
            return f"{int(delta.total_seconds() / 3600)}h ago"
        return f"{delta.days}d ago"
    except (ValueError, TypeError):
        return "—"


# ---------------------------------------------------------------------------
# MCP tool decorator
# ---------------------------------------------------------------------------


@mcp.tool()
async def inbox_digest(
    days: int = 7,
    include_resolved: bool = False,
) -> str:
    """Prioritized digest of recent inbox evaluations and related follow-ups.

    Shows pending action items from inbox evaluations, recently resolved items,
    and completed evaluations. Call this when reviewing inbox evaluation output
    or at the start of a foreground session to catch up on inbox activity.

    Args:
        days: Look-back window in days (default 7)
        include_resolved: Include recently resolved follow-ups (default false)
    """
    result = await _impl_inbox_digest(days=days, include_resolved=include_resolved)
    if "error" in result:
        return result["error"]
    return result["formatted"]
