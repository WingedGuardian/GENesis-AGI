"""Light-reflection topic: a near-duplicate summary is not re-posted.

The stored observation is always kept; only the Telegram post is gated. A
short "no material change" stub goes out at most once per
``LIGHT_NO_CHANGE_STUB_HOURS`` so the topic still proves liveness. "Last
posted" is read back from the outbound ``telegram_messages`` rows TopicManager
already writes, so it survives a restart.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from genesis.awareness.types import Depth, TickResult
from genesis.cc.reflection_bridge._output import (
    LIGHT_NO_CHANGE_STUB_HOURS,
    format_topic_summary,
    send_light_topic_summary,
)
from genesis.cc.types import CCOutput
from genesis.channels.telegram.topics import TopicManager

_CHAT = -100123
_THREAD = 7

# Synthetic summaries (no real install output). Jaccard against _A, by
# compute_jaccard: _A_REWORDED 0.69 (near-duplicate), _A_LOOSE 0.39 (inside
# the model-flag band), _B 0.0 (a real change).
_A = (
    "Disk usage eased to 0.41 (down from 0.44), but the scheduled job "
    "example_sync keeps failing (scheduled_job_health=0.5)."
)
_A_REWORDED = (
    "Disk usage has eased (disk_usage_pct=0.41 down from 0.44) but the "
    "scheduled job example_sync keeps failing, which may delay nightly work."
)
_A_LOOSE = (
    "Disk usage is steady at 0.41 and the scheduled job example_sync is still "
    "failing; queue depth rose slightly."
)
_B = (
    "Genesis version changed (genesis_version_changed=1.0), indicating an "
    "upstream update; a new conversation started since the last reflection."
)


# Telegram message ids are unique per chat; one counter across every fake bot
# keeps a "restarted" manager's sends from colliding on the UNIQUE key.
_IDS = itertools.count(1000)


def _bot():
    bot = AsyncMock()
    topic = MagicMock()
    topic.message_thread_id = _THREAD
    bot.create_forum_topic = AsyncMock(return_value=topic)

    async def _send(**kwargs):
        msg = MagicMock()
        msg.message_id = next(_IDS)
        return msg

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


async def _manager(db, bot=None):
    tm = TopicManager(bot or _bot(), forum_chat_id=_CHAT, db=db)
    await tm.load_persisted()
    return tm


def _raw(assessment: str, **extra) -> str:
    return json.dumps({"assessment": assessment, "observations": [], **extra})


async def _send(tm, assessment, *, now=None, **extra):
    raw = _raw(assessment, **extra)
    text = format_topic_summary(
        Depth.LIGHT,
        CCOutput(
            session_id="s",
            text=raw,
            model_used="haiku",
            cost_usd=0.0,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            exit_code=0,
        ),
    )
    return await send_light_topic_summary(
        "s",
        text,
        source_text=raw,
        topic_manager=tm,
        now=now,
    )


async def _age(db, hours: float) -> None:
    """Move every posted message ``hours`` into the past, as time passing would."""
    cur = await db.execute("SELECT id, timestamp FROM telegram_messages")
    for row_id, ts in await cur.fetchall():
        aged = datetime.fromisoformat(ts) - timedelta(hours=hours)
        await db.execute(
            "UPDATE telegram_messages SET timestamp = ? WHERE id = ?",
            (aged.isoformat(), row_id),
        )
    await db.commit()


_OVER = LIGHT_NO_CHANGE_STUB_HOURS + 0.5


async def test_near_duplicate_is_not_posted(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _A_REWORDED) == "skipped"
    assert tm._bot.send_message.await_count == 1


async def test_changed_summary_is_posted(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _B) == "posted"
    assert tm._bot.send_message.await_count == 2
    assert _B[:30] in tm._bot.send_message.await_args.kwargs["text"]


async def test_model_declared_no_change_is_not_posted(db):
    """``material_change: false`` from the model counts as no change when the
    wording differs too much for the deterministic check alone."""
    from genesis.cc.reflection_bridge._output import (
        LIGHT_MODEL_FLAG_MIN_JACCARD,
        LIGHT_NEAR_DUPLICATE_JACCARD,
        _comparable,
    )
    from genesis.memory.source_verification import compute_jaccard

    # Guard: the fixture really sits between the flag floor and the
    # near-duplicate line, so only the flag can suppress it.
    score = compute_jaccard(_comparable(f"h\n\n{_A}"), _comparable(f"h\n\n{_A_LOOSE}"))
    assert LIGHT_MODEL_FLAG_MIN_JACCARD <= score < LIGHT_NEAR_DUPLICATE_JACCARD
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _A_LOOSE, material_change=False) == "skipped"
    assert tm._bot.send_message.await_count == 1


async def test_model_flag_cannot_hide_text_unlike_the_last_post(db):
    """The model compares against its previous stored finding, which may not
    have been posted; a summary sharing almost nothing with what the user last
    saw is posted whatever the flag says."""
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _B, material_change=False) == "posted"
    assert tm._bot.send_message.await_count == 2


async def test_parse_failure_is_never_the_baseline(db):
    tm = await _manager(db)
    await _send(tm, _A)
    bad = format_topic_summary(Depth.LIGHT, CCOutput(
        session_id="s", text="tool chatter", model_used="haiku", cost_usd=0.0,
        input_tokens=1, output_tokens=1, duration_ms=1, exit_code=0,
    ))
    await send_light_topic_summary("s", bad, source_text="tool chatter", topic_manager=tm)
    # Compared against _A (the last real summary), not the fallback.
    assert await _send(tm, _A_REWORDED) == "skipped"


async def test_other_thread_messages_do_not_reset_the_stub_clock(db):
    from genesis.db.crud.telegram_messages import store

    tm = await _manager(db)
    await _send(tm, _A)
    await _age(db, _OVER)
    await store(
        db, chat_id=_CHAT, message_id=999_999, sender="genesis",
        content="a reply in the thread", thread_id=_THREAD, direction="outbound",
    )
    assert await _send(tm, _A) == "stub"


async def test_future_timestamp_counts_as_no_history(db):
    tm = await _manager(db)
    await _send(tm, _A)
    await _age(db, -2)  # clock skew: the last post looks two hours ahead
    assert await _send(tm, _A) == "stub"


async def test_duplicate_after_interval_posts_one_stub(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    await _age(db, _OVER)
    assert await _send(tm, _A_REWORDED) == "stub"
    stub = tm._bot.send_message.await_args.kwargs["text"]
    assert "No material change since" in stub
    assert "example_sync" not in stub
    # The stub resets the liveness clock: the next duplicate is skipped.
    assert await _send(tm, _A) == "skipped"
    assert tm._bot.send_message.await_count == 2


async def test_duplicate_compares_against_last_full_post_not_the_stub(db):
    tm = await _manager(db)
    await _send(tm, _A)
    await _age(db, _OVER)
    assert await _send(tm, _A) == "stub"
    # Still the same finding a whole interval after the stub: another stub,
    # not a re-post of the full summary.
    await _age(db, _OVER)
    assert await _send(tm, _A) == "stub"
    assert tm._bot.send_message.await_count == 3


async def test_restart_does_not_repost_or_lose_the_stub(db):
    tm = await _manager(db)
    await _send(tm, _A)
    # A fresh manager over the same database is what a restart builds.
    tm2 = await _manager(db)
    assert await _send(tm2, _A_REWORDED) == "skipped"
    await _age(db, _OVER)
    tm3 = await _manager(db)
    assert await _send(tm3, _A) == "stub"
    tm4 = await _manager(db)
    assert await _send(tm4, _A) == "skipped"
    assert tm._bot.send_message.await_count == 1
    assert tm2._bot.send_message.await_count == 0
    assert tm3._bot.send_message.await_count == 1
    assert tm4._bot.send_message.await_count == 0


async def test_unparseable_output_is_always_posted(db):
    """The parse-failure one-liner is a liveness/failure signal, never gated."""
    tm = await _manager(db)
    bad = format_topic_summary(
        Depth.LIGHT,
        CCOutput(
            session_id="s",
            text="tool chatter",
            model_used="haiku",
            cost_usd=0.0,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            exit_code=0,
        ),
    )
    for _ in range(2):
        assert (
            await send_light_topic_summary(
                "s",
                bad,
                source_text="tool chatter",
                topic_manager=tm,
            )
            == "posted"
        )
    assert tm._bot.send_message.await_count == 2


async def test_model_text_cannot_impersonate_a_stub(db):
    """A real assessment that opens with the stub's words is still the last
    full post, so a later duplicate of it is recognised."""
    tm = await _manager(db)
    words = "No material change since yesterday: " + _A
    assert await _send(tm, words) == "posted"
    assert await _send(tm, words) == "skipped"


async def test_no_database_posts_everything(db):
    tm = TopicManager(_bot(), forum_chat_id=_CHAT)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _A) == "posted"


async def test_bridge_keeps_observation_when_post_is_skipped(db):
    """Two identical light reflections: both store their output, one posts."""
    from genesis.cc.reflection_bridge import CCReflectionBridge

    invoker = AsyncMock()
    invoker.run = AsyncMock(
        return_value=CCOutput(
            session_id="r",
            text=_raw(_A),
            model_used="haiku",
            cost_usd=0.0,
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            exit_code=0,
        )
    )
    mgr = AsyncMock()
    mgr.create_background = AsyncMock(return_value={"id": "bg"})
    bridge = CCReflectionBridge(session_manager=mgr, invoker=invoker, db=db)
    tm = await _manager(db)
    bridge.set_topic_manager(tm)
    tick = TickResult(
        tick_id="t",
        timestamp="2026-03-07T12:00:00",
        source="scheduled",
        signals=[],
        scores=[],
        classified_depth=Depth.LIGHT,
        trigger_reason="test",
    )
    with patch(
        "genesis.cc.reflection_bridge._bridge.store_reflection_output",
        new=AsyncMock(),
    ) as store:
        for _ in range(2):
            result = await bridge.reflect(Depth.LIGHT, tick, db=db)
            assert result.success, result.reason
    assert store.await_count == 2
    assert tm._bot.send_message.await_count == 1


async def test_duplicate_inside_interval_stays_silent(db):
    tm = await _manager(db)
    await _send(tm, _A)
    await _age(db, LIGHT_NO_CHANGE_STUB_HOURS - 0.5)
    assert await _send(tm, _A) == "skipped"
