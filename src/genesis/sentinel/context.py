"""Diagnostic context assembly for Sentinel CC sessions.

Assembles a comprehensive snapshot of system health for the CC session
to diagnose and fix infrastructure problems.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from genesis.sentinel.remediation_map import (
    TOOLS,
    available_tools,
    required_tools,
)

logger = logging.getLogger(__name__)


async def assemble_diagnostic_context(
    *,
    alarms: list[Any],
    trigger_source: str,
    trigger_reason: str,
    health_snapshot: dict | None = None,
    remediation_history: list[dict] | None = None,
    scope: frozenset[str] | None = None,
    db=None,
) -> str:
    """Build a diagnostic context string for the Sentinel CC session.

    Includes:
    - Fire alarm details (what triggered the Sentinel) + why each woke it
    - The live remediation-tool inventory available on THIS install
    - Current health alerts
    - Recent remediation outcomes
    - Infrastructure health snapshot
    - Recent observations (if DB available)

    ``scope`` is the set of remediation-tool ids available right now (from
    :func:`available_tools`). The dispatcher computes it once per dispatch and
    threads it in; when omitted it is evaluated here so the function stays
    self-contained for tests and other callers.
    """
    sections: list[str] = []
    now = datetime.now(UTC).isoformat()

    # Live remediation inventory — evaluated once, reused for the per-alarm
    # rationale (below) and the tool-inventory section.
    tool_scope = scope if scope is not None else available_tools()

    # 1. Trigger context
    sections.append(f"## Trigger\n\nSource: {trigger_source}\nReason: {trigger_reason}\nTime: {now}")

    # 2. Fire alarms — each line names the remediation path that made it wake
    #    the Sentinel ("why you were woken"). Direct escalations bypass the
    #    map, so an alarm may have no mapped tools; that is fine.
    if alarms:
        alarm_lines = []
        for a in alarms:
            line = f"- **Tier {a.tier}** [{a.alert_id}] {a.severity}: {a.message}"
            req = required_tools(a.alert_id)
            if req:
                actionable = sorted(req & tool_scope)
                if actionable:
                    line += f"\n  ↳ you can act on this with: {', '.join(actionable)}"
                else:
                    line += (
                        f"\n  ↳ mapped remediation ({', '.join(sorted(req))}) is "
                        "not available on this install — escalate with your diagnosis"
                    )
            alarm_lines.append(line)
        sections.append("## Active Fire Alarms\n\n" + "\n".join(alarm_lines))

    # 2b. Available remediation tools — what the Sentinel can actually do on
    #     THIS install right now. Proposed actions must be achievable with
    #     these; anything else is an escalation, not a fix.
    if tool_scope:
        tool_lines = [
            f"- **{tool.id}** — {tool.description}"
            for tool in TOOLS
            if tool.id in tool_scope
        ]
        if tool_lines:
            sections.append(
                "## Available Remediation Tools\n\n"
                "These are the remediation capabilities available on THIS install "
                "right now. Your `proposed_actions` must be achievable with these; "
                "if the fix needs something not listed here, say so in "
                "`recommendation` and escalate rather than inventing a command.\n\n"
                + "\n".join(tool_lines)
            )

    # 3. Health snapshot
    if health_snapshot:
        # Extract key metrics
        infra = health_snapshot.get("infrastructure", {})
        services = health_snapshot.get("services", {})
        queues = health_snapshot.get("queues", {})
        cc = health_snapshot.get("cc_sessions", {})

        infra_summary = []
        for name, data in infra.items():
            status = data.get("status", "unknown") if isinstance(data, dict) else str(data)
            if status not in ("healthy", "active"):
                infra_summary.append(f"- {name}: {status}")
                if isinstance(data, dict) and "message" in data:
                    infra_summary.append(f"  {data['message']}")
        if infra_summary:
            sections.append("## Infrastructure Issues\n\n" + "\n".join(infra_summary))

        svc_summary = []
        for name, data in services.items():
            if isinstance(data, dict):
                state = data.get("active_state", data.get("status", ""))
                if state not in ("active", "healthy", ""):
                    svc_summary.append(f"- {name}: {state}")
        if svc_summary:
            sections.append("## Service Status\n\n" + "\n".join(svc_summary))

        if queues:
            q_lines = [f"- {k}: {v}" for k, v in queues.items() if v]
            if q_lines:
                sections.append("## Queue Depths\n\n" + "\n".join(q_lines))

        if cc and cc.get("status") != "healthy":
            sections.append(f"## CC Sessions\n\n{json.dumps(cc, indent=2)}")

    # 4. Remediation history
    if remediation_history:
        rem_lines = []
        for outcome in remediation_history[-5:]:  # Last 5
            name = outcome.get("action", outcome.get("name", "unknown"))
            executed = outcome.get("executed", False)
            success = outcome.get("success")
            msg = outcome.get("message", "")
            rem_lines.append(f"- {name}: executed={executed}, success={success}, {msg}")
        sections.append("## Recent Remediation Attempts\n\n" + "\n".join(rem_lines))

    # 5. Recent observations from DB
    if db is not None:
        try:
            cursor = await db.execute(
                """SELECT id, source, type, content, priority, created_at
                   FROM observations
                   WHERE resolved = 0
                   ORDER BY created_at DESC
                   LIMIT 5""",
            )
            rows = await cursor.fetchall()
            if rows:
                # Column order: id=0, source=1, type=2, content=3, priority=4, created_at=5
                obs_lines = []
                for row in rows:
                    content_text = str(row[3] or "")[:80]
                    obs_lines.append(
                        f"- [{content_text}...] source={row[1]}, type={row[2]}, "
                        f"priority={row[4]}, at={row[5]}",
                    )
                sections.append("## Unresolved Observations\n\n" + "\n".join(obs_lines))
        except Exception:
            logger.debug("Failed to query recent observations", exc_info=True)

    # 6. Essential knowledge (recent operational context)
    try:
        ek_path = Path.home() / ".genesis" / "essential_knowledge.md"
        if ek_path.exists():
            ek_text = ek_path.read_text().strip()
            if ek_text:
                sections.append(f"## Essential Knowledge (Recent Context)\n\n{ek_text}")
    except Exception:
        logger.debug("Failed to read essential knowledge", exc_info=True)

    return "\n\n".join(sections)
