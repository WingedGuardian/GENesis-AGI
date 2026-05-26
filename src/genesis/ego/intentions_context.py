"""Shared intentions section builder for both ego context builders.

Renders active intentions as a mandatory-review block in ego context.
Called by both UserEgoContextBuilder and GenesisEgoContextBuilder.
"""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import ego_intentions

logger = logging.getLogger(__name__)


async def build_intentions_section(
    db: aiosqlite.Connection,
    ego_source: str,
) -> str:
    """Build the intentions review section for ego context injection.

    Returns markdown section text, or empty-state guidance when no
    active intentions exist.
    """
    try:
        active = await ego_intentions.list_active(db, ego_source)
    except Exception:
        logger.error("Failed to query ego_intentions", exc_info=True)
        return ""

    if not active:
        return (
            "## Deferred Intentions\n\n"
            "*No active intentions. Create new ones when you identify "
            "actions to defer (e.g., rejected proposals with reopen "
            "conditions, blocked investigations).*\n"
        )

    cap = ego_intentions.MAX_ACTIVE_PER_SOURCE
    remaining = max(0, cap - len(active))

    lines = [
        "## Deferred Intentions (MANDATORY REVIEW)\n",
        f"**{len(active)} active intention(s).** You MUST review each "
        "one and include it in your `intentions.review` output — "
        "action: `keep`, `fire`, `withdraw`, or `renew`.\n",
    ]

    for item in active:
        iid = item["id"]
        content = (item["content"] or "")[:200]
        trigger = (item["trigger_condition"] or "")[:200]
        cycles = item["cycle_count"]
        max_c = item["max_cycles"]
        priority = item["priority"]
        reasoning = (item.get("reasoning") or "")[:150]

        lines.append(
            f"- **[id:{iid}]** [{priority}] (cycle {cycles}/{max_c})"
        )
        lines.append(f"  Content: {content}")
        lines.append(f"  Trigger: {trigger}")
        if reasoning:
            lines.append(f"  Reasoning: {reasoning}")

    lines.append(f"\n*{remaining} slot(s) available for new intentions.*\n")

    return "\n".join(lines)
