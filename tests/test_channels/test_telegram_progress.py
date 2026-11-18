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
    await p.on_event("rate_limit_event", None)
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
    p, _, clock, _ = make(streamer)
    clock.t += 100  # quiet since the last event
    p.start()
    try:
        await asyncio.sleep(0.05)
    finally:
        p.stop()
    assert bot.send_message_draft.await_count >= 1
    assert "still working (100 s)" in bot.send_message_draft.await_args.kwargs["text"]


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
