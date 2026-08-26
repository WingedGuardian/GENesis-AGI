"""Tests for the ego questions channel (B3 — ask-user without approval gate).

Covers:
- _process_questions() — validation, cap, task spawning
- _deliver_question_and_route() — delivery shape, reply → observation +
  reactive signal to the asking ego, timeout → observation only
- Output contracts — both egos advertise the questions[] channel
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from genesis.outreach.types import (
    OutreachCategory,
    OutreachRequest,
    OutreachResult,
    OutreachStatus,
)


def _delivered(delivery_id: str = "d1") -> OutreachResult:
    return OutreachResult(
        outreach_id="o1",
        status=OutreachStatus.DELIVERED,
        channel="telegram",
        message_content="q",
        delivery_id=delivery_id,
        chat_id="123",
        thread_id=None,
    )


def _rejected() -> OutreachResult:
    return OutreachResult(
        outreach_id="o1",
        status=OutreachStatus.REJECTED,
        channel="telegram",
        message_content="q",
    )


@pytest.fixture()
def session():
    """Minimal EgoSession mock with reply-capable outreach pipeline."""
    from genesis.ego.session import EgoSession

    s = object.__new__(EgoSession)
    s._outreach_pipeline = AsyncMock()
    s._outreach_pipeline.submit_and_wait = AsyncMock(return_value=(_delivered(), None))
    s._db = AsyncMock()
    s._source_tag = "user_ego_cycle"
    # object.__new__ skips __init__ — mirror its questions-channel attrs.
    s._question_tasks = set()
    s._reply_signal_sink = None
    return s


async def _drain_question_tasks(s) -> None:
    tasks = getattr(s, "_question_tasks", set())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class TestProcessQuestions:
    @pytest.mark.asyncio
    async def test_question_delivers_via_submit_and_wait(self, session):
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions(
                [{"content": "Should I retire the old digest?", "urgency": "normal"}]
            )
            await _drain_question_tasks(session)

        session._outreach_pipeline.submit_and_wait.assert_called_once()
        request = session._outreach_pipeline.submit_and_wait.call_args[0][0]
        assert isinstance(request, OutreachRequest)
        assert request.category == OutreachCategory.NOTIFICATION
        assert request.signal_type == "ego_question"
        assert request.verbatim is True
        assert "retire the old digest" in request.context

    @pytest.mark.asyncio
    async def test_reply_writes_observation_and_pushes_signal(self, session):
        session._outreach_pipeline.submit_and_wait = AsyncMock(
            return_value=(_delivered(), "Yes, retire it.")
        )
        signals: list[dict] = []
        session.set_reply_signal_sink(signals.append)

        obs_create = AsyncMock()
        with patch("genesis.db.crud.observations.create", new=obs_create):
            await session._process_questions(
                [{"content": "Should I retire the old digest?", "urgency": "normal"}]
            )
            await _drain_question_tasks(session)

        obs_create.assert_called_once()
        content = obs_create.call_args.kwargs["content"]
        assert "Should I retire the old digest?" in content
        assert "Yes, retire it." in content
        assert obs_create.call_args.kwargs["source"] == "ego_question"

        assert len(signals) == 1
        assert signals[0]["type"] == "user_reply"
        assert "Yes, retire it." in signals[0]["summary"]
        assert signals[0]["source"] == "ego_question"

    @pytest.mark.asyncio
    async def test_timeout_writes_unanswered_observation_no_signal(self, session):
        session._outreach_pipeline.submit_and_wait = AsyncMock(return_value=(_delivered(), None))
        signals: list[dict] = []
        session.set_reply_signal_sink(signals.append)

        obs_create = AsyncMock()
        with patch("genesis.db.crud.observations.create", new=obs_create):
            await session._process_questions([{"content": "Ping?", "urgency": "low"}])
            await _drain_question_tasks(session)

        obs_create.assert_called_once()
        assert "unanswered" in obs_create.call_args.kwargs["content"]
        assert signals == []

    @pytest.mark.asyncio
    async def test_cap_two_questions_per_cycle(self, session):
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions(
                [{"content": f"Q{i}?", "urgency": "normal"} for i in range(4)]
            )
            await _drain_question_tasks(session)
        assert session._outreach_pipeline.submit_and_wait.call_count == 2

    @pytest.mark.asyncio
    async def test_invalid_items_skipped(self, session):
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions(
                ["not-a-dict", {"urgency": "high"}, {"content": "   "}]
            )
            await _drain_question_tasks(session)
        session._outreach_pipeline.submit_and_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_undelivered_question_writes_not_delivered_observation(self, session):
        """Governance holding a question back must be VISIBLE to the ego —
        a silent drop would contradict the contract's delivery promise."""
        session._outreach_pipeline.submit_and_wait = AsyncMock(return_value=(_rejected(), None))
        signals: list[dict] = []
        session.set_reply_signal_sink(signals.append)

        obs_create = AsyncMock()
        with patch("genesis.db.crud.observations.create", new=obs_create):
            await session._process_questions([{"content": "Q?", "urgency": "normal"}])
            await _drain_question_tasks(session)

        obs_create.assert_called_once()
        assert obs_create.call_args.kwargs["type"] == "not_delivered"
        assert "NOT delivered" in obs_create.call_args.kwargs["content"]
        assert obs_create.call_args.kwargs["origin_class"] == "first_party"
        assert signals == []

    @pytest.mark.asyncio
    async def test_questions_deliver_sequentially_in_one_task(self, session):
        """Two questions share one task and go out one-at-a-time — two
        concurrent waiters in the same chat make standalone replies
        ambiguous (resolve_scoped_pending resolves only a unique waiter)."""
        in_flight = 0
        max_in_flight = 0

        async def _tracked_wait(request, timeout_s=None):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            return (_delivered(), None)

        session._outreach_pipeline.submit_and_wait = AsyncMock(side_effect=_tracked_wait)
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions(
                [
                    {"content": "Q1?", "urgency": "normal"},
                    {"content": "Q2?", "urgency": "normal"},
                ]
            )
            assert len(session._question_tasks) == 1  # ONE task, not two
            await _drain_question_tasks(session)

        assert session._outreach_pipeline.submit_and_wait.call_count == 2
        assert max_in_flight == 1  # never concurrent

    @pytest.mark.asyncio
    async def test_reply_observation_stamped_owner_origin(self, session):
        """User-authored reply content is channel-stamped owner, never
        allowlisted (provenance contract for _USER_CONTENT_OBS_SOURCES)."""
        session._outreach_pipeline.submit_and_wait = AsyncMock(
            return_value=(_delivered(), "answer")
        )
        obs_create = AsyncMock()
        with patch("genesis.db.crud.observations.create", new=obs_create):
            await session._process_questions([{"content": "Q?", "urgency": "normal"}])
            await _drain_question_tasks(session)
        assert obs_create.call_args.kwargs["origin_class"] == "owner"

    @pytest.mark.asyncio
    async def test_normal_urgency_maps_to_medium_signal_priority(self, session):
        """Cadence _map_priority knows low/medium/high — a raw "normal"
        would fall through to low."""
        session._outreach_pipeline.submit_and_wait = AsyncMock(
            return_value=(_delivered(), "answer")
        )
        signals: list[dict] = []
        session.set_reply_signal_sink(signals.append)
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions([{"content": "Q?", "urgency": "normal"}])
            await _drain_question_tasks(session)
        assert signals[0]["priority"] == "medium"

    @pytest.mark.asyncio
    async def test_no_pipeline_graceful(self, session):
        session._outreach_pipeline = None
        await session._process_questions([{"content": "Q?", "urgency": "normal"}])
        await _drain_question_tasks(session)  # no tasks, no crash

    @pytest.mark.asyncio
    async def test_sink_failure_does_not_raise(self, session):
        session._outreach_pipeline.submit_and_wait = AsyncMock(return_value=(_delivered(), "reply"))

        def _bad_sink(event: dict) -> None:
            raise RuntimeError("sink down")

        session.set_reply_signal_sink(_bad_sink)
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions([{"content": "Q?", "urgency": "normal"}])
            await _drain_question_tasks(session)
        # No exception escaped the task.


class TestOutputContracts:
    def test_user_contract_advertises_questions(self):
        from genesis.ego.user_context import UserEgoContextBuilder

        text = UserEgoContextBuilder._output_contract_section()
        assert '"questions"' in text

    def test_genesis_contract_advertises_questions(self):
        from genesis.ego.genesis_context import GenesisEgoContextBuilder

        text = GenesisEgoContextBuilder._output_contract_section()
        assert '"questions"' in text
