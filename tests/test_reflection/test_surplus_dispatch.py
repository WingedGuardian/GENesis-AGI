"""Tests for surplus task dispatch from deep reflection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.reflection.output_router import OutputRouter, parse_deep_reflection_output
from genesis.reflection.types import DeepReflectionOutput, SurplusTaskRequest

# ── Dataclass tests ──────────────────────────────────────────────────


class TestSurplusTaskRequestDataclass:
    def test_accepts_all_fields(self):
        req = SurplusTaskRequest(
            task_type="code_audit",
            reason="stale code detected",
            priority=0.8,
            drive_alignment="curiosity",
            payload='{"target": "src/genesis"}',
        )
        assert req.task_type == "code_audit"
        assert req.reason == "stale code detected"
        assert req.priority == 0.8
        assert req.drive_alignment == "curiosity"
        assert req.payload == '{"target": "src/genesis"}'

    def test_defaults(self):
        req = SurplusTaskRequest(task_type="memory_audit", reason="needed")
        assert req.priority == 0.5
        assert req.drive_alignment == "competence"
        assert req.payload is None


# ── Parser tests ─────────────────────────────────────────────────────


class TestParserExtractsSurplusTaskRequests:
    def test_extracts_surplus_task_requests(self):
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
            "surplus_task_requests": [
                {
                    "task_type": "memory_audit",
                    "reason": "50+ unresolved observations",
                    "priority": 0.7,
                    "drive_alignment": "competence",
                },
                {
                    "task_type": "code_audit",
                    "reason": "stale imports",
                    "payload": "src/genesis/util",
                },
            ],
        })
        output = parse_deep_reflection_output(raw)
        assert len(output.surplus_task_requests) == 2
        assert output.surplus_task_requests[0].task_type == "memory_audit"
        assert output.surplus_task_requests[0].priority == 0.7
        assert output.surplus_task_requests[1].task_type == "code_audit"
        assert output.surplus_task_requests[1].priority == 0.5  # default
        assert output.surplus_task_requests[1].payload == "src/genesis/util"

    def test_defaults_empty_list_when_absent(self):
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
        })
        output = parse_deep_reflection_output(raw)
        assert output.surplus_task_requests == []

    def test_skips_non_dict_entries(self):
        raw = json.dumps({
            "observations": ["obs1"],
            "confidence": 0.8,
            "surplus_task_requests": ["not a dict", 42],
        })
        output = parse_deep_reflection_output(raw)
        assert output.surplus_task_requests == []


# ── Router tests ─────────────────────────────────────────────────────


class TestRouterEnqueuesSurplusTasks:
    @pytest.mark.asyncio
    async def test_enqueues_valid_task(self):
        mock_queue = AsyncMock()
        mock_queue.enqueue.return_value = "task-id-123"

        router = OutputRouter(surplus_queue=mock_queue)
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            surplus_task_requests=[
                SurplusTaskRequest(
                    task_type="memory_audit",
                    reason="backlog detected",
                    priority=0.7,
                    drive_alignment="competence",
                ),
            ],
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["surplus_tasks_enqueued"] == 1
        mock_queue.enqueue.assert_called_once()
        call_kwargs = mock_queue.enqueue.call_args
        assert call_kwargs.kwargs["priority"] == 0.7
        assert call_kwargs.kwargs["drive_alignment"] == "competence"

    @pytest.mark.asyncio
    async def test_rejects_invalid_task_type(self):
        mock_queue = AsyncMock()
        router = OutputRouter(surplus_queue=mock_queue)
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            surplus_task_requests=[
                SurplusTaskRequest(
                    task_type="totally_invalid_type",
                    reason="should be skipped",
                ),
            ],
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["surplus_tasks_enqueued"] == 0
        mock_queue.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_no_queue_gracefully(self):
        router = OutputRouter(surplus_queue=None)
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            surplus_task_requests=[
                SurplusTaskRequest(
                    task_type="memory_audit",
                    reason="should not crash",
                ),
            ],
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["surplus_tasks_enqueued"] == 0

    @pytest.mark.asyncio
    async def test_enqueue_failure_does_not_crash(self):
        mock_queue = AsyncMock()
        mock_queue.enqueue.side_effect = RuntimeError("DB down")

        router = OutputRouter(surplus_queue=mock_queue)
        output = DeepReflectionOutput(
            observations=["obs1"],
            confidence=0.8,
            surplus_task_requests=[
                SurplusTaskRequest(
                    task_type="code_audit",
                    reason="test error handling",
                ),
            ],
        )

        db = AsyncMock()
        summary = await router.route(output, db)

        assert summary["surplus_tasks_enqueued"] == 0


class TestRuntimePassesQueueToRouter:
    def test_output_router_accepts_surplus_queue(self):
        """OutputRouter constructor accepts and stores surplus_queue."""
        mock_queue = MagicMock()
        router = OutputRouter(surplus_queue=mock_queue)
        assert router._surplus_queue is mock_queue

    def test_output_router_defaults_to_none(self):
        router = OutputRouter()
        assert router._surplus_queue is None
