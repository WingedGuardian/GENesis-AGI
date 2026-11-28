"""The Telegram header names the model that actually ran, and the relay owns it (#4850, #3846)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc.conversation import (
    _CAPPED_AFTER_TOOLS_NOTICE,
    _TRUNCATION_NOTICE,
    ConversationLoop,
    _replay_notice,
    _session_control_block,
)
from genesis.cc.exceptions import CCError, CCMemoryPressureError, CCReplayUnsafeError
from genesis.cc.oom import OOMEvidence
from genesis.cc.rate_limit_park import resume_copy
from genesis.cc.types import CCModel, ChannelType, EffortLevel, StreamEvent
from genesis.channels.telegram import _handler_messages
from genesis.channels.telegram._handler_context import HandlerContext
from genesis.channels.telegram._handler_messages import _with_model_prefix
from genesis.channels.telegram.transport.streaming import DraftStreamer


def _streamer(prefix="[Opus / High]", effort="High"):
    bot = MagicMock()
    bot.send_message_draft = AsyncMock()
    return DraftStreamer(bot=bot, chat_id=1, draft_id=3, prefix=prefix, prefix_effort=effort)


def _on_event(s):
    ctx = HandlerContext.__new__(HandlerContext)
    return HandlerContext.make_on_event(ctx, asyncio.Event(), s)


def test_the_prefix_takes_the_running_model():
    s = _streamer()
    s.set_running_model("Sonnet 5.5")
    assert s._prefix == "[Sonnet 5.5 / High]"


def test_no_prefix_stays_no_prefix():
    s = _streamer(prefix="", effort="")
    s.set_running_model("Sonnet 5.5")
    assert s._prefix == ""


@pytest.mark.asyncio
async def test_the_init_event_retitles_the_header():
    s = _streamer()
    on_event = _on_event(s)
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-sonnet-5-5"}))
    assert s._prefix == "[Sonnet 5.5 / High]"
    await on_event(StreamEvent(event_type="init", raw={}))  # no model: unchanged
    assert s._prefix == "[Sonnet 5.5 / High]"


@pytest.mark.asyncio
async def test_a_failover_peers_init_retitles_a_disabled_streamer():
    """Home init, a rate limit (which disables the draft), then the peer's init.
    The final header names the peer."""
    s = _streamer()
    on_event = _on_event(s)
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-opus-5-5"}))
    # A limit that refuses use; an allowed rate_limit_event no longer halts (#4864).
    rejected = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}
    await on_event(StreamEvent(event_type="rate_limit_event", raw=rejected))
    assert not s.enabled
    await on_event(StreamEvent(event_type="init", raw={"model": "glm-5.3[1m]"}))
    assert s._prefix == "[glm-5.3 / High]"


# ── When the relay shows the header ─────────────────────────────────────────


def _opened(model="opus", effort="xhigh"):
    return StreamEvent(event_type="session_opened", raw={"model": model, "effort": effort})


@pytest.mark.asyncio
async def test_a_session_opening_reply_gets_the_header_in_draft_and_final():
    s = _streamer(prefix="", effort="")
    on_event = _on_event(s)
    await on_event(_opened())
    assert s._prefix == "[Opus / xHigh]"
    s.bot.send_message_draft.assert_awaited()  # the draft shows it at once
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-opus-5-5"}))
    await on_event(StreamEvent(event_type="text", text="The answer."))
    assert s._compose_draft().splitlines()[0] == "[Opus 5.5 / xHigh]"
    assert _with_model_prefix(s, "The answer.") == "[Opus 5.5 / xHigh]\nThe answer."


@pytest.mark.asyncio
async def test_a_gap_reply_that_also_opens_a_session_has_one_header():
    """The gap prefix was read from the row before the turn; the run's own model
    and effort replace it, so there is still exactly one header."""
    s = _streamer(prefix="[Sonnet / High]", effort="High")
    on_event = _on_event(s)
    await on_event(_opened("opus", "low"))
    assert s._prefix == "[Opus / Low]"
    assert _with_model_prefix(s, "Hi") == "[Opus / Low]\nHi"


@pytest.mark.asyncio
async def test_an_ordinary_in_session_reply_has_no_header():
    s = _streamer(prefix="", effort="")
    on_event = _on_event(s)
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-opus-5-5"}))
    assert _with_model_prefix(s, "The answer.") == "The answer."


@pytest.mark.parametrize(
    "reply",
    [
        "The answer.",
        "[Sonnet 5.5 / high]\nThe answer.",
        "[Input / Output]\nx",
        "[see notes] and more\nx",
    ],
)
def test_the_reply_body_is_never_edited(reply):
    """No self-written header is searched for any more: the session is told not to
    write one, so a reply that opens with a bracketed line is left alone."""
    s = _streamer(prefix="[Sonnet 5.5 / High]")
    assert _with_model_prefix(s, reply) == "[Sonnet 5.5 / High]\n" + reply
    assert _with_model_prefix(_streamer(prefix="", effort=""), reply) == reply


@pytest.mark.parametrize(
    "reply",
    [
        "[Contingency mode via backup — CC limits reached]\n\nhi",
        "[Genesis timed out — try a simpler request]",
        "[Genesis error: boom]",
        "[MCP error (memory) — try again]",
        _CAPPED_AFTER_TOOLS_NOTICE,
        _TRUNCATION_NOTICE,
        *(resume_copy(mode, None) for mode in ("live", "propose_only", "off")),
        _replay_notice(CCReplayUnsafeError("stopped after tools ran")),
        _replay_notice(
            CCMemoryPressureError(
                OOMEvidence(**dict.fromkeys(OOMEvidence.__dataclass_fields__)), cause=CCError("x")
            )
        ),
        "Model: Opus | Effort: high",
        "Effort: low",
        "Settings unchanged.",
    ],
)
def test_a_reply_the_loop_wrote_gets_no_header(reply):
    """It was not written by the model a header would name."""
    assert _with_model_prefix(_streamer(prefix="[Opus 5.5 / High]"), reply) == reply


def test_every_settings_confirmation_is_recognised():
    from genesis.cc.conversation import _settings_confirmation, is_non_model_reply
    from genesis.cc.types import CCModel, EffortLevel

    for model in (None, *CCModel):
        for effort in (None, *EffortLevel):
            assert is_non_model_reply(_settings_confirmation(model, effort))


@pytest.mark.parametrize(
    "reply",
    ["Model: logistic regression fits best here.", "Effort: matters more than talent.",
     "Model: Opus | Effort: high, and here is why", "Settings unchanged. Also, about X"],
)
def test_an_answer_that_starts_like_a_confirmation_keeps_its_header(reply):
    """Only the exact confirmation is the loop's own reply (review finding)."""
    s = _streamer(prefix="[Opus 5.5 / High]")
    assert _with_model_prefix(s, reply) == "[Opus 5.5 / High]\n" + reply


def _ctx_for_gap(prev_ts, *, drafts=True):
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=None if prev_ts is None else (prev_ts,))
    db = MagicMock()
    db.execute = AsyncMock(return_value=cursor)
    adapter = SimpleNamespace(_app=SimpleNamespace(bot=MagicMock(send_message_draft=AsyncMock())))
    return SimpleNamespace(draft_streaming_enabled=drafts, adapter=adapter, db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize(("chat_type", "drafts"), [("group", True), ("supergroup", True), ("private", False)])
async def test_without_drafts_the_final_reply_still_gets_the_header(chat_type, drafts):
    """Group and topic chats, and drafts turned off, send no drafts. The session is
    still told not to write a header, so the relay must still print one."""
    ctx = _ctx_for_gap(None, drafts=drafts)
    msg = SimpleNamespace(message_id=42, chat=SimpleNamespace(type=chat_type, id=-5), message_thread_id=9)
    s = await _handler_messages._make_streamer(ctx, msg, SimpleNamespace(id=7), "9")
    assert not s.enabled
    on_event = _on_event(s)
    await on_event(_opened("opus", "high"))
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-opus-5-5"}))
    await on_event(StreamEvent(event_type="text", text="The answer."))
    assert _with_model_prefix(s, "The answer.") == "[Opus 5.5 / High]\nThe answer."
    ctx.adapter._app.bot.send_message_draft.assert_not_called()
    assert not s.any_draft_sent


@pytest.mark.asyncio
@pytest.mark.parametrize(("seconds_ago", "expected"), [(7200, "[Opus / High]"), (60, "")])
async def test_the_gap_rule(monkeypatch, seconds_ago, expected):
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import cc_sessions

    monkeypatch.setattr(
        cc_sessions,
        "get_active_foreground",
        AsyncMock(return_value={"model": "opus", "effort": "high"}),
    )
    prev = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    msg = SimpleNamespace(message_id=42, chat=SimpleNamespace(type="private", id=1), message_thread_id=None)
    s = await _handler_messages._make_streamer(_ctx_for_gap(prev), msg, SimpleNamespace(id=7), None)
    assert s._prefix == expected


# ── The conversation loop announces a session-opening run ─────────────────


async def _run(was_resume, *, fail_resume=False):
    events: list[StreamEvent] = []

    async def on_event(ev):
        events.append(ev)

    invoker = MagicMock()
    if fail_resume:
        invoker.run_streaming = AsyncMock(side_effect=[CCError("stale"), "out"])
    else:
        invoker.run_streaming = AsyncMock(return_value="out")
    fake = SimpleNamespace(
        _invoker=invoker,
        _recover_stale_resume=AsyncMock(return_value={"id": "s2"}),
        _build_fresh_invocation=AsyncMock(return_value=MagicMock()),
    )
    await ConversationLoop._try_invoke_streaming(
        fake,
        MagicMock(session_key=None),
        session={"id": "s1"},
        was_resume=was_resume,
        prompt_text="hi",
        model=CCModel.OPUS,
        effort=EffortLevel.HIGH,
        user_id="tg-1",
        channel=ChannelType.TELEGRAM,
        thread_id=None,
        on_event=on_event,
    )
    return [e for e in events if e.event_type == "session_opened"]


@pytest.mark.asyncio
async def test_a_fresh_run_announces_session_opened():
    opened = await _run(was_resume=False)
    assert [e.raw for e in opened] == [{"model": "opus", "effort": "high"}]


@pytest.mark.asyncio
async def test_a_resumed_run_does_not():
    assert await _run(was_resume=True) == []


@pytest.mark.asyncio
async def test_a_stale_resume_retried_fresh_announces_it():
    assert len(await _run(was_resume=True, fail_resume=True)) == 1


# ── The session is told not to write its own header ───────────────────────


def test_telegram_sessions_are_told_the_channel_prints_the_header():
    block = _session_control_block(ChannelType.TELEGRAM, CCModel.OPUS, EffortLevel.HIGH, "s1")
    assert "do not begin a reply with a [model / effort] line" in block


@pytest.mark.parametrize("channel", [ChannelType.TERMINAL, ChannelType.WEB])
def test_other_channels_are_not(channel):
    assert _session_control_block(channel, CCModel.OPUS, EffortLevel.HIGH, "s1") == ""


def test_the_header_line_survives_the_failover_strip():
    """A failover peer loses the home model/effort block but keeps the header rule,
    which is true for whichever model answers."""
    from genesis.cc.conversation import _strip_session_control_block

    block = _session_control_block(ChannelType.TELEGRAM, CCModel.OPUS, EffortLevel.HIGH, "s1")
    stripped = _strip_session_control_block("base" + block)
    assert "Changing your own model" not in stripped
    assert "do not begin a reply with a [model / effort] line" in stripped


@pytest.mark.asyncio
async def test_one_init_names_the_model_and_starts_a_new_progress_attempt():
    """The header handlers return on init, so the progress ledger must see the
    event first: a retry's init both renames the header and starts a fresh
    attempt, or the retry is judged against the failed attempt's ledger."""
    from genesis.channels.telegram._progress import TurnProgress

    s = _streamer()
    sent: list[str] = []

    async def deliver(text: str) -> None:
        sent.append(text)

    progress = TurnProgress(deliver, s, asyncio.Event())
    ctx = HandlerContext.__new__(HandlerContext)
    on_event = HandlerContext.make_on_event(ctx, asyncio.Event(), s, progress)
    await on_event(StreamEvent(event_type="text", text="Text from the failed attempt."))
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-sonnet-5-5"}))
    assert s._prefix == "[Sonnet 5.5 / High]"
    assert progress.pending() == ""  # the new attempt starts with an empty ledger


# ── Effort only where the model uses it (#4869 review, round 4) ──────────────


@pytest.mark.asyncio
async def test_a_haiku_run_header_names_no_effort():
    """The invoker never passes Haiku an effort setting, so its header shows none,
    whether it comes from session_opened or from the init event."""
    s = _streamer(prefix="", effort="")
    on_event = _on_event(s)
    await on_event(StreamEvent(event_type="session_opened", raw={"model": "haiku", "effort": "high"}))
    assert s._prefix == "[Haiku]"
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-haiku-4-5"}))
    assert s._prefix == "[Haiku 4.5]"


@pytest.mark.asyncio
async def test_a_run_handed_to_haiku_drops_the_effort_and_a_later_model_regains_it():
    s = _streamer(prefix="[Sonnet / High]", effort="High")
    on_event = _on_event(s)
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-haiku-4-5"}))
    assert s._prefix == "[Haiku 4.5]"
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-opus-5-5"}))
    assert s._prefix == "[Opus 5.5 / High]"


@pytest.mark.asyncio
async def test_the_gap_prefix_for_a_haiku_session_names_no_effort(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import cc_sessions

    monkeypatch.setattr(
        cc_sessions, "get_active_foreground", AsyncMock(return_value={"model": "haiku", "effort": "high"})
    )
    prev = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    msg = SimpleNamespace(message_id=42, chat=SimpleNamespace(type="private", id=1), message_thread_id=None)
    s = await _handler_messages._make_streamer(_ctx_for_gap(prev), msg, SimpleNamespace(id=7), None)
    assert s._prefix == "[Haiku]"


# ── The gap rule in group and topic chats (#4869 review, round 4) ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("chat_type", "tid"), [("group", None), ("supergroup", "9")])
async def test_the_gap_rule_applies_in_groups_and_topics(monkeypatch, chat_type, tid):
    """After an hour of silence in this chat and topic, a group or topic reply
    carries the header too; the lookup is scoped to the chat and the topic."""
    from datetime import UTC, datetime, timedelta

    from genesis.db.crud import cc_sessions

    monkeypatch.setattr(
        cc_sessions, "get_active_foreground", AsyncMock(return_value={"model": "opus", "effort": "high"})
    )
    prev = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    ctx = _ctx_for_gap(prev)
    msg = SimpleNamespace(message_id=42, chat=SimpleNamespace(type=chat_type, id=-5), message_thread_id=int(tid) if tid else None
    )
    s = await _handler_messages._make_streamer(ctx, msg, SimpleNamespace(id=7), tid)
    assert s._prefix == "[Opus / High]"
    sql, params = ctx.db.execute.await_args.args
    assert "thread_id IS ?" in sql
    assert params == (-5, int(tid) if tid else None, 42)



@pytest.mark.asyncio
async def test_the_gap_rule_ignores_the_current_message_whenever_it_is_stored(monkeypatch):
    """Voice and media store the inbound message only after the streamer is built,
    text before it. On a real table, a message two minutes ago means no header,
    whether or not the current one is stored yet (#4869 review)."""
    from datetime import UTC, datetime, timedelta

    import aiosqlite

    from genesis.db.crud import cc_sessions

    monkeypatch.setattr(
        cc_sessions, "get_active_foreground", AsyncMock(return_value={"model": "opus", "effort": "high"})
    )
    now = datetime.now(UTC)
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE telegram_messages (chat_id INTEGER, message_id INTEGER, thread_id INTEGER,"
            " direction TEXT, timestamp TEXT)"
        )
        rows = [(1, 1, None, "inbound", (now - timedelta(hours=3)).isoformat()),
                (1, 2, None, "inbound", (now - timedelta(minutes=2)).isoformat())]
        await db.executemany("INSERT INTO telegram_messages VALUES (?, ?, ?, ?, ?)", rows)
        ctx = _ctx_for_gap(None)
        ctx.db = db
        msg = SimpleNamespace(message_id=3, chat=SimpleNamespace(type="private", id=1), message_thread_id=None)
        s = await _handler_messages._make_streamer(ctx, msg, SimpleNamespace(id=7), None)
        assert s._prefix == ""  # voice: the current message is not stored yet
        await db.execute("INSERT INTO telegram_messages VALUES (1, 3, NULL, 'inbound', ?)", (now.isoformat(),))
        s = await _handler_messages._make_streamer(ctx, msg, SimpleNamespace(id=7), None)
        assert s._prefix == ""  # text: it is
