"""System-computed ego focus summary.

Derives focus from DB state (directives, goals, board) rather than
ego-authored text. Eliminates self-reinforcing behavioral loops where
the ego encodes holdback states ("demo silence", "panel support posture")
into focus_summary, which persists across cycles via ego_state.

The ego's authored focus is still logged in ego_cycles for audit but
is NOT stored in ego_state. This module produces what gets stored.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def compute_focus_summary(
    db: aiosqlite.Connection,
    ego_key: str,
) -> str:
    """Derive a factual focus summary from current DB state.

    Parameters
    ----------
    db:
        Database connection.
    ego_key:
        Either ``"ego_focus_summary"`` (user ego) or
        ``"genesis_ego_focus_summary"`` (genesis ego).

    Returns a one-liner describing active directives, board state,
    and goals — purely factual, no behavioral interpretation.
    """
    from genesis.db.crud import ego as ego_crud

    parts: list[str] = []

    try:
        # Active directives
        ego_target = (
            "user_ego" if "genesis" not in ego_key else "genesis_ego"
        )
        directives = await ego_crud.list_active_directives(
            db, ego_target=ego_target, limit=5,
        )
        if directives:
            summaries = [
                d.get("content", "")[:60].split(".")[0]
                for d in directives[:3]
            ]
            parts.append(
                f"{len(directives)} directive(s): {'; '.join(summaries)}"
            )

        # Proposal board
        board = await ego_crud.get_board(db, board_size=5)
        if board:
            topics = [
                p.get("content", "")[:40].split(".")[0]
                for p in board[:3]
            ]
            parts.append(
                f"{len(board)} pending proposal(s): {'; '.join(topics)}"
            )

        # User ego: active goals. Genesis ego: skip (operational focus).
        if "genesis" not in ego_key:
            from genesis.db.crud import user_goals

            goals = await user_goals.list_active(db, limit=5)
            if goals:
                titles = [g.get("title", "")[:40] for g in goals[:3]]
                suffix = f" (+{len(goals) - 3} more)" if len(goals) > 3 else ""
                parts.append(f"goals: {', '.join(titles)}{suffix}")

        # Recently approved (last 3)
        approved = await ego_crud.list_proposals(
            db, status="approved", limit=3,
        )
        if approved:
            parts.append(f"{len(approved)} approved awaiting dispatch")

    except Exception:
        logger.warning("compute_focus_summary failed, using fallback", exc_info=True)
        return "general system awareness"

    return "; ".join(parts) if parts else "no active directives or proposals"
