"""Snapshot functions for HealthDataService.

Each function takes explicit dependencies as parameters and returns a dict.
Used by HealthDataService.snapshot() to build the unified health response.
"""

from __future__ import annotations

from genesis.observability.snapshots.api_keys import api_key_health, validate_api_keys
from genesis.observability.snapshots.awareness import awareness
from genesis.observability.snapshots.call_sites import call_sites
from genesis.observability.snapshots.cc_sessions import cc_sessions
from genesis.observability.snapshots.conversation import conversation_activity
from genesis.observability.snapshots.cost import cost
from genesis.observability.snapshots.infrastructure import (
    infrastructure,
    resilience_state,
)
from genesis.observability.snapshots.outreach import outreach_stats
from genesis.observability.snapshots.proactive_memory import proactive_memory_metrics
from genesis.observability.snapshots.provider_activity import provider_activity
from genesis.observability.snapshots.queues import queues
from genesis.observability.snapshots.services import mcp_status, services
from genesis.observability.snapshots.surplus import surplus_status

__all__ = [
    "api_key_health",
    "awareness",
    "call_sites",
    "cc_sessions",
    "conversation_activity",
    "cost",
    "infrastructure",
    "mcp_status",
    "outreach_stats",
    "proactive_memory_metrics",
    "provider_activity",
    "queues",
    "resilience_state",
    "services",
    "surplus_status",
    "validate_api_keys",
]
