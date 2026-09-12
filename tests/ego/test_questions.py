"""Tests for the ego questions channel (B3 — ask-user without approval gate).

Covers:
- _process_questions() — validation, cap, task spawning
- _deliver_question_and_route() — delivery shape, reply → observation +
  reactive signal to the asking ego, timeout → observation only
- Output contracts — both egos advertise the questions[] channel
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

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
    # Reply-wait infra present by default (P2#10 guard); sync so it isn't a coroutine.
    s._outreach_pipeline.supports_reply_wait = Mock(return_value=True)
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
    async def test_timeout_writes_unanswered_observation_and_wakes_ego(self, session):
        session._outreach_pipeline.submit_and_wait = AsyncMock(return_value=(_delivered(), None))
        signals: list[dict] = []
        session.set_reply_signal_sink(signals.append)

        obs_create = AsyncMock()
        with patch("genesis.db.crud.observations.create", new=obs_create):
            await session._process_questions([{"content": "Ping?", "urgency": "low"}])
            await _drain_question_tasks(session)

        obs_create.assert_called_once()
        assert "unanswered" in obs_create.call_args.kwargs["content"]
        # P1-c-3: the asking ego must LEARN the no-reply outcome. The 4h context
        # section is opportunistic (a cycle may not run inside the window before
        # the ~2h timeout row ages out), so a reactive signal wakes the ego.
        # Previously this asserted signals == [] — that WAS the bug.
        assert len(signals) == 1
        assert signals[0]["type"] == "question_no_reply"
        assert signals[0]["source"] == "ego_question"

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
        # P1-c-3: a delivery failure must also wake the asking ego so it can
        # reason about a retry (previously asserted signals == [] — the bug).
        assert len(signals) == 1
        assert signals[0]["type"] == "question_not_delivered"
        assert signals[0]["source"] == "ego_question"

    @pytest.mark.asyncio
    async def test_questions_deliver_sequentially_in_one_task(self, session):
        """Two questions share one task and go out one-at-a-time — two
        concurrent waiters in the same chat make standalone replies
        ambiguous (resolve_scoped_pending resolves only a unique waiter)."""
        in_flight = 0
        max_in_flight = 0

        async def _tracked_wait(request, timeout_s=None, standalone_resolvable=True):
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


class TestQuestionHardening:
    """Codex #1499 remediation — DM-capture, non-string content, obs-fail."""

    @pytest.mark.asyncio
    async def test_delivery_is_quote_reply_only(self, session):
        """Questions go into the owner's general DM — a bare unrelated message
        must NOT be consumed as the answer, so the waiter is quote-reply-only
        (standalone_resolvable=False)."""
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions([{"content": "Q?", "urgency": "normal"}])
            await _drain_question_tasks(session)
        kwargs = session._outreach_pipeline.submit_and_wait.call_args.kwargs
        assert kwargs.get("standalone_resolvable") is False

    @pytest.mark.asyncio
    async def test_non_string_content_skipped_without_crash(self, session):
        """A non-string content value (malformed JSON) must be skipped, never
        crash _process_questions on .strip()."""
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions(
                [
                    {"content": {"nested": 1}, "urgency": "normal"},
                    {"content": ["a", "b"], "urgency": "low"},
                    {"content": 42, "urgency": "high"},
                ]
            )
            await _drain_question_tasks(session)
        session._outreach_pipeline.submit_and_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_signal_fires_even_if_observation_write_fails(self, session):
        """A durable-write failure must not swallow the captured answer — the
        reactive signal to the asking ego still fires."""
        session._outreach_pipeline.submit_and_wait = AsyncMock(
            return_value=(_delivered(), "the answer")
        )
        signals: list[dict] = []
        session.set_reply_signal_sink(signals.append)
        failing_obs = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("genesis.db.crud.observations.create", new=failing_obs):
            await session._process_questions([{"content": "Q?", "urgency": "high"}])
            await _drain_question_tasks(session)
        assert len(signals) == 1
        assert "the answer" in signals[0]["summary"]


def test_ego_question_never_deduped():
    """topic is content[:100]; ANY non-zero window suppresses a distinct question
    sharing a 100-char prefix (and a re-ask reminder). Must be 0 (Codex #1499)."""
    from genesis.outreach.governance import _DEDUP_WINDOWS

    assert _DEDUP_WINDOWS["ego_question"] == 0


@pytest.mark.asyncio
async def test_ego_question_section_surfaces_answer():
    """The asking USER ego must SEE the answer to a question it asked — the
    reactive signal alone doesn't render into the prompt, so the ego_question
    observation reader is the durable channel (Codex #1499 P1)."""
    from datetime import UTC, datetime

    import aiosqlite

    from genesis.db.schema import TABLES
    from genesis.ego.user_context import UserEgoContextBuilder

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(TABLES["observations"])
        await db.commit()
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, category, created_at, resolved) "
            "VALUES ('o1','ego_question','user_reply',?,?,'ego_question',?,0)",
            (
                "Ego asked: Retire the old digest?\nUser replied: Yes, retire it.",
                "medium",
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()

        builder = object.__new__(UserEgoContextBuilder)
        builder._db = db
        section = await builder._ego_question_section()

    assert "Answers To Your Questions" in section
    assert "Yes, retire it." in section


def test_ego_qa_is_an_always_section():
    """The answer-delivery section must be ALWAYS-rendered: a reply arrives AS a
    reactive signal → a reactive cycle, and the reactive profile trims most
    sections. Resting the delivery guarantee on the section_map's .get default
    would break silently the moment ego_qa entered a trim profile (Codex #1499
    NOTE-A)."""
    from genesis.ego import focus

    assert "ego_qa" in focus._ALL_SECTIONS
    assert "ego_qa" in focus._ALWAYS_SECTIONS
    # And it resolves to "always" (never trimmed) in the reactive profile.
    assert focus.FOCUS_CONTEXT_WEIGHTS["reactive"]["ego_qa"] == "always"


class TestQuestionRemediation2:
    """Codex #1499 round-2 remediation — P2#7/#8/#10 (P1-b deferred: f/u 8cc2c200)."""

    @pytest.mark.asyncio
    async def test_delivered_text_carries_quote_reply_instruction(self, session):
        """P2#8: the waiter is quote-reply-only, so the DELIVERED text must tell
        the user to reply-to-the-message; a plain next-DM answer hits no waiter.
        `content` (what the observations record) stays clean — the instruction
        lives only in the outreach `context`."""
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions([{"content": "Ship it?", "urgency": "normal"}])
            await _drain_question_tasks(session)
        request = session._outreach_pipeline.submit_and_wait.call_args[0][0]
        assert "Ship it?" in request.context
        assert "reply to this message" in request.context.lower()

    @pytest.mark.asyncio
    async def test_distinct_questions_get_unique_topic(self, session):
        """P2#7: two DISTINCT questions sharing a 100-char lead-in must not
        collapse via the pipeline in-flight guard (_awaited_dup_key keys on
        signal_type+topic) — each question gets a unique topic identity."""
        prefix = "X" * 100
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions(
                [
                    {"content": prefix + " ALPHA", "urgency": "normal"},
                    {"content": prefix + " BETA", "urgency": "normal"},
                ]
            )
            await _drain_question_tasks(session)
        calls = session._outreach_pipeline.submit_and_wait.call_args_list
        assert len(calls) == 2
        topics = {c[0][0].topic for c in calls}
        assert len(topics) == 2, f"topics collided: {topics}"

    @pytest.mark.asyncio
    async def test_questions_skipped_without_reply_wait_infra(self, session):
        """P2#10: submit_and_wait silently falls back to fire-and-forget when the
        pipeline has no reply-waiter — so a question could be 'sent' but never
        answerable. Skip (with a log) rather than deliver an un-answerable prompt."""
        session._outreach_pipeline.supports_reply_wait = Mock(return_value=False)
        with patch("genesis.db.crud.observations.create", new=AsyncMock()):
            await session._process_questions([{"content": "Q?", "urgency": "normal"}])
            await _drain_question_tasks(session)
        session._outreach_pipeline.submit_and_wait.assert_not_called()


def test_static_identity_schemas_advertise_questions():
    """P2#5: the STATIC identity schemas the model sees every cycle must include
    `questions` — not only the dynamic context contract — or the two disagree."""
    from pathlib import Path

    import genesis

    identity = Path(genesis.__file__).parent / "identity"
    for name in ("USER_EGO_SESSION.md", "GENESIS_EGO_SESSION.md"):
        text = (identity / name).read_text()
        assert '"questions"' in text, f"{name} static schema omits questions[]"


@pytest.mark.asyncio
async def test_ego_question_section_excludes_stale_rows():
    """P2#6: the 4h window must actually bound. Stored created_at is ISO-8601
    ('T' separator); a raw text compare against datetime('now',...) (space
    separator) wrongly includes same-day old rows ('T' > ' '). A 5h-old reply
    must be EXCLUDED."""
    from datetime import UTC, datetime, timedelta

    import aiosqlite

    from genesis.db.schema import TABLES
    from genesis.ego.user_context import UserEgoContextBuilder

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(TABLES["observations"])
        await db.commit()
        stale_ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        await db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, category, created_at, resolved) "
            "VALUES ('o_old','ego_question','user_reply',?,?,'ego_question',?,0)",
            ("Ego asked: stale?\nUser replied: old answer.", "medium", stale_ts),
        )
        await db.commit()

        builder = object.__new__(UserEgoContextBuilder)
        builder._db = db
        section = await builder._ego_question_section()

    assert section == "", f"5h-old row leaked past the 4h window: {section!r}"
