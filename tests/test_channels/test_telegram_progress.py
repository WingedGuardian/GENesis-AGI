"""TurnProgress: interim delivery, the delivery ledger, the snapshot and the draft
keepalive for one Telegram turn (#4849)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.channels.telegram import _progress
from genesis.channels.telegram._progress import TurnProgress
from genesis.channels.telegram.transport.streaming import DraftStreamer

LONG = "I found the record you asked about; reading the rest now."  # >= 40 chars
SHORT = "Checking."
# A rate_limit_event that refuses use; Claude Code also emits allowed ones.
REJECTED = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(streamer=None, *, fail=False):
    sent: list[str] = []

    async def deliver(text: str) -> None:
        if fail:
            raise RuntimeError("send failed")
        sent.append(text)

    clock = Clock()
    interrupt = asyncio.Event()
    return TurnProgress(deliver, streamer, interrupt, clock=clock), sent, clock, interrupt


@pytest.mark.asyncio
async def test_text_before_a_tool_call_is_delivered_at_the_tool_boundary():
    p, sent, _, _ = make()
    await p.on_event("text", LONG)
    assert sent == []  # nothing before the tool call: the text may still grow
    await p.on_event("tool_use", None)
    assert sent == [LONG] and p.delivered == LONG


@pytest.mark.asyncio
async def test_a_short_aside_is_held_and_the_answer_appears_once():
    """The reviewer's repro: an aside, a tool call, then the answer. The answer's own
    text events must not be held as well, or the user reads it twice."""
    p, sent, _, _ = make()
    await p.on_event("text", SHORT)
    await p.on_event("tool_use", None)
    await p.on_event("text", "The answer.")
    assert sent == []
    assert p.final_reply("The answer.") == f"{SHORT}\n\nThe answer."


@pytest.mark.asyncio
async def test_progress_is_paced_and_held_text_goes_at_the_next_boundary():
    p, sent, clock, _ = make()
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Second step: " + LONG)
    clock.t += 3
    await p.on_event("tool_use", None)
    assert len(sent) == 1  # inside the interval: held
    clock.t += _progress.MIN_PROGRESS_INTERVAL_S
    await p.on_event("tool_use", None)
    assert len(sent) == 2 and sent[1].startswith("Second step:")


@pytest.mark.asyncio
async def test_text_held_by_pacing_reaches_the_final_reply_once():
    p, sent, clock, _ = make()
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Second step: " + LONG)
    clock.t += 3
    await p.on_event("tool_use", None)
    await p.on_event("text", "Final answer.")
    reply = p.final_reply("Final answer.")
    assert reply == f"Second step: {LONG}\n\nFinal answer."
    assert reply.count("Final answer.") == 1 and LONG not in sent[0][len(LONG):]


@pytest.mark.asyncio
async def test_delivered_text_is_never_sent_again():
    p, _, _, _ = make()
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", "\nFinal answer.")
    # A rescue sends what is pending; the empty-result fallback's reply is all the
    # streamed text joined. Neither repeats the delivered part.
    assert p.pending() == "Final answer."
    assert p.final_reply(LONG + "\nFinal answer.") == "Final answer."


@pytest.mark.asyncio
async def test_the_normal_reply_is_left_whole():
    p, _, _, _ = make()
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Final answer.")
    assert p.final_reply("Final answer.") == "Final answer."


@pytest.mark.asyncio
async def test_two_held_asides_are_not_repeated_by_the_fallback_or_a_rescue():
    """The second review's repro: two short asides held, then the answer. The
    empty-result fallback's reply (all text joined, raw) and a rescue (pending)
    already carry them, so nothing is put in front again."""
    p, _, _, _ = make()
    for text in ("Checking.", "Reading."):
        await p.on_event("text", text)
        await p.on_event("tool_use", None)
    await p.on_event("text", "The cause is X.")
    joined = "Checking.Reading.The cause is X."
    assert p.final_reply(joined) == joined
    p2, _, _, _ = make()
    for text in ("Checking.", "Reading."):
        await p2.on_event("text", text)
        await p2.on_event("tool_use", None)
    await p2.on_event("text", "Half")
    rescue = f"{p2.pending()}\n\nNotice."
    assert p2.final_reply(rescue) == rescue


@pytest.mark.asyncio
async def test_a_reply_that_quotes_the_aside_still_gets_it_in_front():
    p, _, _, _ = make()
    await p.on_event("text", "Checking.")
    await p.on_event("tool_use", None)
    await p.on_event("text", "Done. Checking. confirmed it.")
    assert p.final_reply("Done. Checking. confirmed it.") == "Checking.\n\nDone. Checking. confirmed it."


@pytest.mark.asyncio
async def test_a_failed_delivery_keeps_its_text_for_the_final_reply():
    p, _, _, _ = make(fail=True)
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Done.")
    assert p.delivered == ""
    assert p.final_reply("Done.") == f"{LONG}\n\nDone."


@pytest.mark.asyncio
async def test_a_rate_limit_event_stops_progress_and_keeps_the_text(monkeypatch):
    """The rate-limit double-send class: once the stream reports a rate limit or an
    error, nothing more goes out mid-turn; held text waits for the final reply."""
    p, sent, _, _ = make()
    await p.on_event("rate_limit_event", None, raw=REJECTED)
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    assert sent == []
    assert p.final_reply("Ok.") == f"{LONG}\n\nOk."


@pytest.mark.asyncio
async def test_pending_does_not_depend_on_the_draft_streamer():
    """A rescue after the draft streamer stopped (rate limit, send failures) still
    sends everything not delivered, and nothing that was."""
    streamer, _ = _streamer()
    p, _, _, _ = make(streamer)
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    streamer.disable()
    await p.on_event("text", "Late text.")
    assert p.pending() == "Late text."


@pytest.mark.asyncio
async def test_finish_stops_a_pending_snapshot(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 0.02)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 3600)
    streamer, _ = _streamer()
    await streamer.on_tool_use("Bash")
    p, sent, _, _ = make(streamer)
    p.start()
    await p.on_event("tool_use", None)
    p.finish()
    await asyncio.sleep(0.06)
    assert sent == []


@pytest.mark.asyncio
async def test_an_interrupted_turn_delivers_nothing():
    p, sent, _, interrupt = make()
    interrupt.set()
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    assert sent == []


def _streamer(prefix="[Sonnet / High]"):
    bot = MagicMock()
    bot.send_message_draft = AsyncMock()
    return DraftStreamer(bot=bot, chat_id=1, draft_id=3, prefix=prefix), bot


@pytest.mark.asyncio
async def test_snapshot_waits_for_the_first_event_and_skips_a_bare_prefix(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 0.01)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 3600)
    streamer, _ = _streamer()
    p, sent, _, _ = make(streamer)
    p.start()
    try:
        await p.on_event("init", None)
        await asyncio.sleep(0.05)
        assert sent == []  # only the prefix to show: nothing worth posting
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_snapshot_clock_starts_at_the_first_event_not_at_receipt(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 0.01)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 3600)
    streamer, _ = _streamer()
    await streamer.on_tool_use("Read")  # something a snapshot would show
    p, sent, _, _ = make(streamer)
    p.start()
    try:
        await asyncio.sleep(0.05)
        assert sent == []  # Claude Code has not started yet (cold start)
        await p.on_event("tool_use", None)
        await asyncio.sleep(0.05)
        assert len(sent) == 1
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_snapshot_posts_tool_activity_without_the_prefix(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 0.01)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 3600)
    streamer, _ = _streamer()
    await streamer.on_tool_use("Bash", "ls the logs")
    p, sent, _, _ = make(streamer)
    p.start()
    try:
        await p.on_event("tool_use", None)
        await asyncio.sleep(0.05)
        assert len(sent) == 1 and "Bash" in sent[0] and "[Sonnet" not in sent[0]
        assert "ls the logs" in sent[0]
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_snapshot_is_skipped_once_progress_was_delivered(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 0.01)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 3600)
    streamer, _ = _streamer()
    await streamer.on_tool_use("Bash")
    p, sent, _, _ = make(streamer)
    p.start()
    try:
        await p.on_event("text", LONG)
        await p.on_event("tool_use", None)
        await asyncio.sleep(0.05)
        assert sent == [LONG]
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_keepalive_refreshes_a_quiet_draft(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 3600)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 0.01)
    streamer, bot = _streamer(prefix="")
    await streamer.on_thinking()
    bot.send_message_draft.reset_mock()
    p, _, _, _ = make(streamer)
    streamer._last_send_time -= 100  # the draft has been quiet 100 s
    p.start()
    try:
        await asyncio.sleep(0.05)
    finally:
        p.stop()
    assert bot.send_message_draft.await_count >= 1
    # The first refresh reports the quiet stretch; it restarts the draft's clock.
    first = bot.send_message_draft.await_args_list[0].kwargs["text"]
    assert "still working (100 s)" in first


@pytest.mark.asyncio
async def test_any_event_clears_the_waiting_line():
    streamer, bot = _streamer(prefix="")
    await streamer.keepalive(40)
    await streamer.on_text("Now writing.")
    assert "still working" not in streamer._compose_draft()


def test_draft_throttle_stays_under_the_per_chat_limit():
    # 30 s / 1.5 s = 20 draft calls, plus 7.5 typing calls at a 4 s keepalive: < 40.
    assert DraftStreamer(bot=MagicMock(), chat_id=1, draft_id=1).throttle_s >= 1.5


def test_tool_preview_names_what_the_call_works_on():
    from genesis.channels.telegram._handler_context import _tool_preview

    assert _tool_preview({"command": "git status", "description": "Show status"}) == "Show status"
    assert _tool_preview({"file_path": "/a/b.py"}) == "/a/b.py"
    assert _tool_preview(None) == "" and _tool_preview({"x": 1}) == ""


@pytest.mark.asyncio
async def test_the_text_handler_delivers_progress_then_a_final_reply_without_repeats(
    monkeypatch,
):
    """End to end through the real handler: the model writes, calls a tool, writes
    its answer. The user gets the first text as a logged progress message, then the
    answer, and nothing twice."""
    from genesis.cc.types import StreamEvent
    from genesis.channels.telegram import _handler_messages as hm
    from genesis.channels.telegram.handlers_v2 import make_handlers_v2

    replies: list[str] = []
    persisted: list[tuple[str, str]] = []

    async def fake_reply(msg, text):
        replies.append(text)
        return MagicMock(message_id=len(replies))

    async def fake_persist(ctx, chat_id, message_id, sender, content, **kw):
        persisted.append((sender, content))

    monkeypatch.setattr(hm, "_reply_formatted", fake_reply)
    monkeypatch.setattr(hm, "_persist_tg_message", fake_persist)

    async def stream(prompt, *, on_event, **kw):
        await on_event(StreamEvent(event_type="init", raw={}))
        await on_event(StreamEvent(event_type="text", text=LONG))
        await on_event(StreamEvent(event_type="tool_use", tool_name="Read", tool_input={}))
        await on_event(StreamEvent(event_type="text", text="Final answer."))
        return "Final answer."

    loop = AsyncMock(spec=["handle_message_streaming", "_db"])
    loop.handle_message_streaming = AsyncMock(side_effect=stream)
    loop._db = None
    adapter = MagicMock()
    adapter.get_chat_lock = MagicMock(return_value=asyncio.Lock())
    adapter._app.bot.send_message_draft = AsyncMock(return_value=True)
    handlers = make_handlers_v2(loop, allowed_users={123}, whisper_model="base", adapter=adapter)

    update = MagicMock()
    update.effective_user.id = 123
    msg = update.message
    msg.text, msg.message_id, msg.message_thread_id = "what happened?", 7, None
    msg.reply_to_message = None
    msg.chat.id, msg.chat.type = 456, "private"
    msg.chat.send_action = AsyncMock()
    msg.reply_text = AsyncMock()
    update.effective_message = msg

    await handlers["text"](update, MagicMock())

    assert replies[0] == LONG
    assert replies[-1].endswith("Final answer.") and LONG not in replies[-1]
    assert (hm.PROGRESS_SENDER, LONG) in persisted


async def _drive_text_handler(monkeypatch, stream):
    """Run the real text handler over a scripted stream; return (replies, persisted)."""
    from genesis.channels.telegram import _handler_messages as hm
    from genesis.channels.telegram.handlers_v2 import make_handlers_v2

    replies: list[str] = []
    persisted: list[tuple[str, str]] = []

    async def fake_reply(msg, text):
        replies.append(text)
        return MagicMock(message_id=len(replies))

    async def fake_persist(ctx, chat_id, message_id, sender, content, **kw):
        persisted.append((sender, content))

    monkeypatch.setattr(hm, "_reply_formatted", fake_reply)
    monkeypatch.setattr(hm, "_persist_tg_message", fake_persist)
    loop = AsyncMock(spec=["handle_message_streaming", "_db"])
    loop.handle_message_streaming = AsyncMock(side_effect=stream)
    loop._db = None
    adapter = MagicMock()
    adapter.get_chat_lock = MagicMock(return_value=asyncio.Lock())
    adapter._app.bot.send_message_draft = AsyncMock(return_value=True)
    handlers = make_handlers_v2(loop, allowed_users={123}, whisper_model="base", adapter=adapter)
    update = MagicMock()
    update.effective_user.id = 123
    msg = update.message
    msg.text, msg.message_id, msg.message_thread_id = "what happened?", 7, None
    msg.reply_to_message = None
    msg.chat.id, msg.chat.type = 456, "private"
    msg.chat.send_action = AsyncMock()
    msg.reply_text = AsyncMock()
    update.effective_message = msg
    await handlers["text"](update, MagicMock())
    return replies, persisted


@pytest.mark.asyncio
async def test_a_dropped_connection_rescues_only_what_was_not_delivered(monkeypatch):
    from genesis.cc.types import StreamEvent

    async def stream(prompt, *, on_event, **kw):
        await on_event(StreamEvent(event_type="text", text=LONG))
        await on_event(StreamEvent(event_type="tool_use", tool_name="Read", tool_input={}))
        await on_event(StreamEvent(event_type="text", text="Half an answer"))
        raise ConnectionError("dropped")

    replies, _ = await _drive_text_handler(monkeypatch, stream)
    assert replies == [LONG, "Half an answer"]


@pytest.mark.asyncio
async def test_a_dropped_connection_after_everything_was_delivered_says_so(monkeypatch):
    from genesis.cc.types import StreamEvent

    async def stream(prompt, *, on_event, **kw):
        await on_event(StreamEvent(event_type="text", text=LONG))
        await on_event(StreamEvent(event_type="tool_use", tool_name="Read", tool_input={}))
        raise ConnectionError("dropped")

    replies, _ = await _drive_text_handler(monkeypatch, stream)
    assert replies[0] == LONG and "connection dropped" in replies[-1] and len(replies) == 2


@pytest.mark.asyncio
async def test_a_short_aside_then_the_answer_reads_once_through_the_handler(monkeypatch):
    """The blocker, end to end: the final message carries the aside and the answer
    once each."""
    from genesis.cc.types import StreamEvent

    async def stream(prompt, *, on_event, **kw):
        await on_event(StreamEvent(event_type="text", text=SHORT))
        await on_event(StreamEvent(event_type="tool_use", tool_name="Read", tool_input={}))
        await on_event(StreamEvent(event_type="text", text="The answer."))
        return "The answer."

    replies, _ = await _drive_text_handler(monkeypatch, stream)
    assert len(replies) == 1 and replies[0].count("The answer.") == 1
    assert replies[0].startswith(SHORT)


# --- The delivery ledger across every path that sends text (PR #4864 review) ---


def _formatted(text: str) -> str:
    """What handle_message_streaming returns for ``text`` on Telegram."""
    from genesis.cc.formatter import ResponseFormatter
    from genesis.cc.types import ChannelType

    return "\n".join(ResponseFormatter().format(text, channel=ChannelType.TELEGRAM))


def _long_segment(n: int = 5059) -> str:
    # Numbered words, so any repeated span is a real repeat.
    return " ".join(f"word{i}" for i in range(n // 5))[:n]


@pytest.mark.asyncio
async def test_progress_longer_than_one_message_goes_out_in_parts():
    """Held text longer than one Telegram message is sent as several, as one unit:
    once every part is accepted all of it is delivered, so neither the final reply
    nor the empty-result fallback (every text joined, split, re-joined) repeats it,
    and later progress keeps flowing."""
    segment = _long_segment()
    p, sent, clock, _ = make()
    await p.on_event("text", segment)
    await p.on_event("tool_use", None)
    assert len(sent) > 1 and "".join(sent) == segment and p.delivered == segment
    clock.t += _progress.MIN_PROGRESS_INTERVAL_S
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Final answer.")
    assert sent[-1] == LONG
    assert p.final_reply("Final answer.") == "Final answer."
    assert p.final_reply(_formatted(segment + LONG + "Final answer.")) == "Final answer."


@pytest.mark.asyncio
async def test_a_part_that_fails_keeps_all_of_the_held_text_for_the_final_reply():
    """The held text is delivered only when every part is accepted. When a later
    part fails, all of it stays held, so the final reply carries it whole: the
    parts already sent appear twice, the accepted cost of not tracking how much of
    a split text got through. No snapshot follows what is already on screen."""
    segment = _long_segment()
    sent: list[str] = []

    async def deliver(text: str) -> None:
        if sent:
            raise RuntimeError("second part failed")
        sent.append(text)

    p = TurnProgress(deliver, None, asyncio.Event(), clock=Clock())
    await p.on_event("text", segment)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Done.")
    assert len(sent) == 1 and segment.startswith(sent[0])
    assert p.delivered == "" and p.any_delivered
    assert p.final_reply("Done.") == f"{segment}\n\nDone."


@pytest.mark.asyncio
async def test_a_long_code_block_reaches_the_user_with_balanced_fences():
    """A fence longer than one message is force-split with balanced fences; once
    all of it is delivered, the final reply and fallback do not repeat it."""
    code = "Here is the code:\n```\n" + "x = 1\n" * 900 + "```\n"
    p, sent, _, _ = make()
    await p.on_event("text", code)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Final answer.")
    assert len(sent) > 1 and all(part.count("```") % 2 == 0 for part in sent)
    assert p.final_reply(_formatted(code + "Final answer.")) == "Final answer."


@pytest.mark.asyncio
async def test_a_long_fallback_reply_with_held_text_past_a_chunk_keeps_it_once():
    segment = _long_segment()
    p, sent, clock, _ = make()
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", segment)
    await p.on_event("tool_use", None)  # inside the pacing interval: held
    await p.on_event("text", "Final answer.")
    assert sent == [LONG]
    reply = p.final_reply(_formatted(LONG + segment + "Final answer."))
    assert reply.count(segment[:200]) == 1 and LONG not in reply
    assert reply.endswith("Final answer.")


def _synthetic(text: str):
    from genesis.cc.types import StreamEvent

    return StreamEvent(
        event_type="text",
        text=text,
        raw={"type": "assistant", "message": {"model": "<synthetic>"}},
    )


@pytest.mark.asyncio
async def test_a_failover_reply_does_not_inherit_the_home_quota_notice(monkeypatch):
    """Cap, rate_limit_event, then a peer that narrates, calls a tool and answers.
    The CLI's own quota notice is not narration, and the peer is a new attempt:
    its progress is delivered and its reply carries nothing of the home attempt."""
    from genesis.cc.types import StreamEvent

    notice = "You've hit your weekly limit · resets 5pm (UTC)"

    async def stream(prompt, *, on_event, **kw):
        await on_event(StreamEvent(event_type="init", raw={}))
        await on_event(_synthetic(notice))
        await on_event(StreamEvent(event_type="rate_limit_event", raw=REJECTED))
        await on_event(StreamEvent(event_type="init", raw={}))  # the peer
        await on_event(StreamEvent(event_type="text", text=LONG))
        await on_event(StreamEvent(event_type="tool_use", tool_name="Read", tool_input={}))
        await on_event(StreamEvent(event_type="text", text="The requested answer."))
        return "The requested answer."

    replies, _ = await _drive_text_handler(monkeypatch, stream)
    assert replies[0] == LONG
    assert replies[-1].endswith("The requested answer.")
    assert all(notice not in r for r in replies)
    assert sum(r.count(LONG) for r in replies) == 1


@pytest.mark.asyncio
async def test_a_rate_limit_still_stops_progress_for_its_attempt():
    p, sent, _, _ = make()
    await p.on_event("init", None)
    await p.on_event("text", LONG)
    await p.on_event("rate_limit_event", None, raw=REJECTED)
    await p.on_event("tool_use", None)
    assert sent == []
    assert p.final_reply("Parked.") == f"{LONG}\n\nParked."


@pytest.mark.asyncio
async def test_a_retry_keeps_a_ledger_of_its_own():
    """A stale-resume retry is a new Claude Code run: it starts a new ledger, so
    what it delivers is never repeated in its own final reply or fallback. Text an
    EARLIER attempt delivered is not this ledger's to suppress: repeats across
    attempts belong to the streaming retry (#4888)."""
    other = "Starting over: the record is in the second table, reading it."
    p, sent, clock, _ = make()
    for event, text in (
        ("init", None), ("text", LONG), ("tool_use", None), ("text", "Half"),
        ("error", None), ("init", None), ("text", other),
    ):
        await p.on_event(event, text)
    clock.t += _progress.MIN_PROGRESS_INTERVAL_S  # past the pacing interval
    await p.on_event("tool_use", None)
    await p.on_event("text", "Final answer.")
    assert sent == [LONG, other] and p.delivered == other
    assert p.final_reply("Final answer.") == "Final answer."
    assert p.final_reply(other + "Final answer.") == "Final answer."
    # A retry that re-streams what the failed attempt delivered sends it again.
    p2, sent2, clock2, _ = make()
    for event, text in (
        ("init", None), ("text", LONG), ("tool_use", None), ("error", None),
        ("init", None), ("text", LONG),
    ):
        await p2.on_event(event, text)
    clock2.t += _progress.MIN_PROGRESS_INTERVAL_S
    await p2.on_event("tool_use", None)
    assert sent2 == [LONG, LONG]


@pytest.mark.asyncio
async def test_a_retry_drops_what_the_failed_attempt_only_held():
    p, sent, _, _ = make()
    await p.on_event("init", None)
    await p.on_event("text", SHORT)
    await p.on_event("tool_use", None)
    await p.on_event("error", None)
    await p.on_event("init", None)
    await p.on_event("text", "The answer.")
    assert p.final_reply("The answer.") == "The answer."
    assert sent == []


@pytest.mark.asyncio
async def test_a_contingency_reply_gets_no_held_text_in_front():
    """Contingency answers the turn again, tool-less: like a retry it is a new
    attempt, and its header must stay first for the model-prefix check."""
    from genesis.cc.conversation import CONTINGENCY_HEADER_PREFIX

    p, _, _, _ = make()
    await p.on_event("text", SHORT)
    await p.on_event("tool_use", None)
    await p.on_event("rate_limit_event", None, raw=REJECTED)
    reply = f"{CONTINGENCY_HEADER_PREFIX} via X]\n\nAnswer."
    assert p.final_reply(reply) == reply


@pytest.mark.asyncio
async def test_a_failed_progress_send_is_held_whole_for_the_final_reply(monkeypatch):
    """A progress send is one message, accepted or not: when Telegram refuses it,
    none of it counts as delivered, and the final reply carries all of it once."""
    from genesis.channels.telegram import _handler_messages as hm

    segment = _long_segment(3000)
    calls: list[str] = []

    async def reply_text(text, **kw):
        calls.append(text)
        raise RuntimeError("send failed")

    msg = MagicMock()
    msg.chat.type, msg.chat.id = "private", 456
    msg.reply_text = reply_text
    ctx = MagicMock()
    ctx.db = None
    progress = hm._make_progress(ctx, msg, None, None, asyncio.Event())
    try:
        await progress.on_event("text", segment)
        await progress.on_event("tool_use", None)
        await progress.on_event("text", "Done.")
    finally:
        progress.finish()
    assert calls == [segment]  # one message, the whole held text
    assert progress.delivered == "" and progress.pending().startswith(segment)
    reply = progress.final_reply("Done.")
    assert reply == f"{segment}\n\nDone." and reply.count(segment[:100]) == 1


@pytest.mark.asyncio
async def test_a_halt_stops_the_snapshot(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 0.01)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 3600)
    streamer, _ = _streamer()
    await streamer.on_tool_use("Bash", "ls the logs")
    p, sent, _, _ = make(streamer)
    p.start()
    try:
        await p.on_event("tool_use", None)
        await p.on_event("rate_limit_event", None, raw=REJECTED)
        await asyncio.sleep(0.05)
        assert sent == []
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_a_halt_stops_the_keepalive(monkeypatch):
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 3600)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 0.01)
    streamer, bot = _streamer(prefix="")
    await streamer.on_thinking()
    bot.send_message_draft.reset_mock()
    p, _, clock, _ = make(streamer)
    await p.on_event("error", None)
    clock.t += 100
    p.start()
    try:
        await asyncio.sleep(0.05)
    finally:
        p.stop()
    assert bot.send_message_draft.await_count == 0


@pytest.mark.asyncio
def _limit(status: str) -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status}}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["allowed", "allowed_warning"])
async def test_an_informational_rate_limit_event_changes_nothing(monkeypatch, status):
    """Claude Code emits rate_limit_event on ordinary turns; only a rejection is a
    limit. Progress, the snapshot and the keepalive all carry on."""
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 0.01)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 0.01)
    streamer, bot = _streamer(prefix="")
    await streamer.on_tool_use("Bash", "ls the logs")
    bot.send_message_draft.reset_mock()
    p, sent, clock, _ = make(streamer)
    p.start()
    try:
        await p.on_event("tool_use", None)
        await p.on_event("rate_limit_event", None, raw=_limit(status))
        clock.t += 100
        await asyncio.sleep(0.05)
        assert len(sent) == 1 and sent[0].startswith("Working on it:")  # the snapshot
        assert bot.send_message_draft.await_count >= 1  # the keepalive
    finally:
        p.stop()
    p2, sent2, _, _ = make()
    await p2.on_event("text", LONG)
    await p2.on_event("rate_limit_event", None, raw=_limit(status))
    await p2.on_event("tool_use", None)
    await p2.on_event("text", "Final answer.")
    assert sent2 == [LONG]
    assert p2.final_reply("Final answer.") == "Final answer."


@pytest.mark.asyncio
async def test_an_overage_rejection_is_not_a_halt():
    p, sent, _, _ = make()
    raw = {"type": "rate_limit_event",
           "rate_limit_info": {"status": "rejected", "isUsingOverage": True}}
    await p.on_event("text", LONG)
    await p.on_event("rate_limit_event", None, raw=raw)
    await p.on_event("tool_use", None)
    assert sent == [LONG]


@pytest.mark.asyncio
async def test_an_allowed_rate_limit_event_keeps_the_draft_through_the_handler(monkeypatch):
    from genesis.cc.types import StreamEvent
    from genesis.channels.telegram._handler_context import HandlerContext

    streamer, _ = _streamer(prefix="")
    ctx = HandlerContext.__new__(HandlerContext)
    on_event = HandlerContext.make_on_event(ctx, asyncio.Event(), streamer, None)
    await on_event(StreamEvent(event_type="rate_limit_event", raw=_limit("allowed")))
    assert streamer.enabled
    await on_event(StreamEvent(event_type="rate_limit_event", raw=REJECTED))
    assert not streamer.enabled


@pytest.mark.asyncio
async def test_subagent_lines_are_not_progress_or_boundaries():
    """The Agent tool's subagent streams its own assistant lines, marked with a
    parent_tool_use_id: not this turn's narration, and not its tool calls."""
    sub = {"type": "assistant", "parent_tool_use_id": "toolu_1"}
    p, sent, _, _ = make()
    await p.on_event("text", LONG)
    await p.on_event("text", "Subagent narration that is long enough to send.", raw=sub)
    await p.on_event("tool_use", None, raw=sub)
    assert sent == []  # a subagent tool call is not a boundary of this turn
    await p.on_event("tool_use", None)
    assert sent == [LONG]
    await p.on_event("text", "Final answer.")
    assert p.final_reply("Final answer.") == "Final answer."


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_dropped_connection_with_nothing_delivered_says_so_plainly(monkeypatch):
    """Text was seen but nothing reached the user (here: a retry dropped the failed
    attempt's held aside): no "messages above" to point at."""
    from genesis.cc.types import StreamEvent

    async def stream(prompt, *, on_event, **kw):
        await on_event(StreamEvent(event_type="init", raw={}))
        await on_event(StreamEvent(event_type="text", text=SHORT))
        await on_event(StreamEvent(event_type="tool_use", tool_name="Read", tool_input={}))
        await on_event(StreamEvent(event_type="init", raw={}))
        raise ConnectionError("dropped")

    replies, _ = await _drive_text_handler(monkeypatch, stream)
    assert replies == []


SUB = {"type": "assistant", "parent_tool_use_id": "toolu_1"}


@pytest.mark.asyncio
async def test_a_fallback_reply_with_subagent_text_does_not_repeat_a_held_aside():
    """The invoker's empty-result fallback joins every text event, a subagent's
    included; progress leaves subagent text out. The reply must still be recognised,
    so the held aside appears once."""
    p, sent, _, _ = make()
    await p.on_event("init", None)
    await p.on_event("text", LONG)
    await p.on_event("tool_use", None)
    await p.on_event("text", "Subagent internal narration. ", raw=SUB)
    await p.on_event("tool_use", None, raw=SUB)
    await p.on_event("text", "Short aside.")
    await p.on_event("tool_use", None)  # inside the pacing interval: held
    await p.on_event("text", "Answer.")
    assert sent == [LONG]
    fallback = LONG + "Subagent internal narration. " + "Short aside." + "Answer."
    reply = p.final_reply(_formatted(fallback))
    assert reply.count("Short aside.") == 1 and "Subagent" not in reply
    assert reply.endswith("Answer.") and LONG not in reply


@pytest.mark.asyncio
async def test_a_long_fallback_reply_with_two_subagent_bursts_is_recognised():
    segment = _long_segment()
    p, sent, clock, _ = make()
    stream = [
        ("init", None, None), ("text", LONG, None), ("tool_use", None, None),
        ("text", "First subagent burst. ", SUB), ("tool_use", None, SUB),
        ("text", "Short aside.", None), ("tool_use", None, None),
        ("text", segment, SUB), ("tool_use", None, SUB),
        ("text", "Second aside.", None), ("tool_use", None, None),
        ("text", "Answer.", None),
    ]
    for event, text, raw in stream:
        await p.on_event(event, text, raw=raw)
    assert sent == [LONG]
    fallback = "".join(text for event, text, _ in stream if event == "text")
    reply = p.final_reply(_formatted(fallback))
    # The fallback is the turn's own text as streamed (as in the two-asides case).
    assert reply == "Short aside.Second aside.Answer."


@pytest.mark.asyncio
async def test_the_draft_leaves_out_subagent_lines():
    from genesis.cc.types import StreamEvent
    from genesis.channels.telegram._handler_context import HandlerContext

    streamer, _ = _streamer(prefix="")
    ctx = HandlerContext.__new__(HandlerContext)
    on_event = HandlerContext.make_on_event(ctx, asyncio.Event(), streamer, None)
    await on_event(StreamEvent(event_type="text", text="Main text.", raw={"type": "assistant"}))
    await on_event(StreamEvent(event_type="text", text="Subagent text.", raw=SUB))
    await on_event(StreamEvent(event_type="tool_use", tool_name="Grep", tool_input={}, raw=SUB))
    await on_event(StreamEvent(event_type="tool_use", tool_name="Read", tool_input={}, raw={}))
    draft = streamer._compose_draft()
    assert "Main text." in draft and "Subagent" not in draft
    assert "Read" in draft and "Grep" not in draft


@pytest.mark.asyncio
async def test_events_the_draft_does_not_show_do_not_hold_off_its_keepalive(monkeypatch):
    """Quiet is measured on the draft, not the stream. A subagent's events and the
    turn's own tool results arrive more often than the keepalive interval and
    change nothing on screen, so the keepalive must still refresh the draft, or
    it expires mid-task (#4864 review)."""
    monkeypatch.setattr(_progress, "SNAPSHOT_AFTER_S", 3600)
    monkeypatch.setattr(_progress, "KEEPALIVE_S", 0.05)
    streamer, bot = _streamer(prefix="")
    await streamer.on_tool_use("Agent", "delegate the search")
    bot.send_message_draft.reset_mock()
    p, _, _, _ = make(streamer)
    p.start()
    try:
        for _ in range(40):  # an event every 10 ms for 0.4 s
            await p.on_event("text", "child narration", raw=SUB)
            await p.on_event("tool_result", None, raw={"type": "user"})
            await asyncio.sleep(0.01)
    finally:
        p.finish()
    assert bot.send_message_draft.await_count > 0
