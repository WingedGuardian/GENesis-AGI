"""Infrastructure monitor — the Sentinel's proactive eyes (Call Site 37).

Runs every awareness tick on free models (Gemini Flash / Haiku). Evaluates
the full health landscape with LLM judgment and can wake the Sentinel when
it spots patterns that programmatic thresholds miss.

The monitor OBSERVES and JUDGES. It never acts. It creates observations and
wakes the Sentinel. The Sentinel ACTS.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from genesis.sentinel.dispatcher import SentinelDispatcher

logger = logging.getLogger(__name__)

# 37_infrastructure_monitor — TEMPORARILY DISABLED in commit ff2198c (2026-05-03).
# Sentinel infrastructure here remains in place; output quality from free-model
# providers was too low for surplus dispatch. Rework pending: higher-tier providers
# and tighter prompts before re-enable.
CALL_SITE_ID = "37_infrastructure_monitor"


async def run_infrastructure_monitor(
    *,
    health_snapshot: dict[str, Any] | None = None,
    sentinel: SentinelDispatcher | None = None,
    router=None,
    db=None,
) -> dict[str, Any] | None:
    """Run the infrastructure monitor on a free model.

    Args:
        health_snapshot: Current system health from HealthDataService.snapshot()
        sentinel: The Sentinel dispatcher (to wake if needed)
        router: LLM router for dispatching the API call
        db: Database connection for observations

    Returns:
        The monitor's assessment dict, or None if the call failed.
    """
    if router is None or health_snapshot is None:
        return None

    # Build the monitor prompt
    prompt = _build_monitor_prompt(health_snapshot)

    try:
        # Dispatch via API on free model (call site 37)
        response = await router.call(
            call_site_id=CALL_SITE_ID,
            messages=[{"role": "user", "content": prompt}],
        )
        if response is None:
            return None

        # Parse the response
        text = response.get("content", "") if isinstance(response, dict) else str(response)
        assessment = _parse_monitor_output(text)

        if assessment is None:
            return None

        # Log assessment
        status = assessment.get("status", "unknown")
        if status != "ok":
            logger.info(
                "Infrastructure monitor: status=%s, wake_sentinel=%s, reason=%s",
                status,
                assessment.get("wake_sentinel", False),
                assessment.get("reason", "")[:100],
            )

        # Wake the Sentinel if the monitor judges it necessary
        if assessment.get("wake_sentinel") and sentinel is not None:
            from genesis.sentinel.dispatcher import SentinelRequest
            from genesis.util.tasks import tracked_task

            tracked_task(
                sentinel.dispatch(SentinelRequest(
                    trigger_source="infrastructure_monitor",
                    trigger_reason=assessment.get("reason", "monitor judgment"),
                    tier=2,  # Monitor-triggered = Tier 2 (pattern, not threshold)
                )),
                name="sentinel-monitor-wake",
            )

        # Create observation if the monitor saw something concerning
        if status == "concerning" and db is not None:
            try:
                import uuid

                from genesis.db.crud import observations
                await observations.create(
                    db,
                    id=f"infra-monitor-{uuid.uuid4().hex[:8]}",
                    source="infrastructure_monitor",
                    type="health_assessment",
                    content=json.dumps(assessment),
                    priority="low",
                    created_at=datetime.now(UTC).isoformat(),
                )
            except Exception:
                logger.error("Failed to create monitor observation", exc_info=True)

        return assessment

    except Exception:
        logger.error("Infrastructure monitor call failed", exc_info=True)
        return None


def _build_monitor_prompt(health_snapshot: dict[str, Any]) -> str:
    """Build the assessment prompt from the health snapshot."""
    # Extract relevant sections
    infra = health_snapshot.get("infrastructure", {})
    services = health_snapshot.get("services", {})
    queues = health_snapshot.get("queues", {})
    cc = health_snapshot.get("cc_sessions", {})

    sections = [
        "You are the infrastructure monitor for Genesis, an autonomous AI system.",
        "Evaluate the following health snapshot and assess whether any patterns",
        "are concerning. Look for combinations of signals that individually may",
        "not cross thresholds but together suggest emerging problems.",
        "",
        "## Current Health Snapshot",
        "",
        f"Infrastructure: {json.dumps(infra, indent=2)}",
        f"Services: {json.dumps(services, indent=2)}",
        f"Queues: {json.dumps(queues, indent=2)}",
        f"CC Sessions: {json.dumps(cc, indent=2)}",
        "",
        "## Your Assessment",
        "",
        "Respond with ONLY a JSON object:",
        '{"status": "ok|concerning|alarm", "observations": ["..."], '
        '"wake_sentinel": false, "reason": "..."}',
        "",
        "- status: ok (normal), concerning (worth noting), alarm (needs investigation)",
        "- observations: list of things you noticed",
        "- wake_sentinel: true ONLY if you judge immediate investigation is needed",
        "- reason: why you set wake_sentinel (required if true)",
    ]
    return "\n".join(sections)


def _parse_monitor_output(text: str) -> dict[str, Any] | None:
    """Parse the monitor's JSON output, tolerant of formatting."""
    if not text:
        return None

    # Try to find JSON in the output
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    # Try parsing the whole text as JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code blocks
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue

    return None
