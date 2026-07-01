"""web_agent must NOT auto-deny on an exceeded budget (Design Principle 3).

Cost is observability, never automatic control. When the daily budget is
EXCEEDED the tool logs a warning and PROCEEDS to the agent call instead of
returning an error — verified here by asserting the adapter is reached.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

from genesis.mcp.health.web_tools import web_agent


@asynccontextmanager
async def _fake_db(*_a, **_k):
    yield object()


class _ExceededTracker:
    def __init__(self, _db):
        pass

    async def check_budget(self):
        return "EXCEEDED"


class _AdapterReached:
    """Adapter stub whose failure result proves control reached the agent call
    rather than short-circuiting on the budget check."""

    class _Result:
        success = False
        error = "ADAPTER_REACHED"
        data = None

    async def invoke(self, _payload):
        return self._Result()


async def test_web_agent_proceeds_when_budget_exceeded(monkeypatch):
    monkeypatch.setenv("API_KEY_TINYFISH", "test-key")
    with (
        patch("genesis.db.connection.get_raw_db", _fake_db),
        patch("genesis.routing.cost_tracker.CostTracker", _ExceededTracker),
        patch("genesis.providers.tinyfish_agent.TinyFishAgentAdapter", _AdapterReached),
    ):
        result = await web_agent.fn(url="https://example.com", goal="do a thing")

    # Reached the adapter (proceeded) — NOT the old budget short-circuit.
    assert result["error"] == "ADAPTER_REACHED"
    assert "budget" not in str(result).lower()
