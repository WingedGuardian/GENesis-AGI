"""Shared user-directives section builder for both ego context builders.

Renders active directives (from ``ego_directives``) targeted at a given ego as
a context block. Called by both UserEgoContextBuilder and
GenesisEgoContextBuilder — only the ``ego_target``, the framing sentence, and
the error body differ, so the query + age-computation + render loop live here
once (mirrors the ``build_intentions_section`` shared-helper pattern).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import ego as ego_crud

logger = logging.getLogger(__name__)

_HEADER = "## User Directives"


async def build_directives_section(
    db: aiosqlite.Connection,
    ego_target: str,
    *,
    framing: str,
    error_body: str,
) -> str:
    """Render active directives for ``ego_target`` as a markdown block.

    Returns ``""`` when there are none (no empty header pollutes the context).

    Parameters
    ----------
    ego_target:
        ``"user_ego"`` or ``"genesis_ego"`` — whose directives to render.
    framing:
        The intro sentence shown under the header, including its own trailing
        newline (each ego frames directives slightly differently).
    error_body:
        The italic line rendered when the directive query fails.
    """
    try:
        directives = await ego_crud.list_active_directives(
            db,
            ego_target=ego_target,
            limit=5,
        )
    except Exception:
        logger.warning("Failed to query %s directives", ego_target, exc_info=True)
        return f"{_HEADER}\n\n{error_body}\n"

    if not directives:
        return ""

    lines = [f"{_HEADER}\n", framing]

    now = datetime.now(UTC)
    for d in directives:
        priority = d.get("priority", "normal").upper()
        content = d.get("content", "?")[:200]
        directive_id = d.get("id", "?")
        created_at = d.get("created_at", "")
        age_str = ""
        if created_at:
            try:
                created = datetime.fromisoformat(created_at)
                delta = now - created
                if delta.days > 0:
                    age_str = f"{delta.days}d ago"
                else:
                    hours = int(delta.total_seconds() / 3600)
                    age_str = f"{hours}h ago" if hours > 0 else "just now"
            except (ValueError, TypeError):
                pass
        age_part = f", {age_str}" if age_str else ""
        lines.append(f"- [{priority}] {content}\n  (id={directive_id}{age_part})")

    lines.append("")
    return "\n".join(lines)
