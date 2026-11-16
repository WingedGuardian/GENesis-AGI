"""Light-reflection topic: a summary the model flags as unchanged is not re-posted.

Only an explicit ``"material_change": false`` suppresses a post; an absent flag
always posts. The flag is trusted only while code finds no condition change
(an alarm started or cleared, a band crossed, a signal appeared or went away)
between the run behind the last full post and this one; wording is never
compared. The stored observation is always kept; only the Telegram post is gated. A
short "no material change" stub goes out at most once per
``LIGHT_NO_CHANGE_STUB_HOURS`` so the topic still proves liveness. "Last
posted" is read back from the outbound ``telegram_messages`` rows TopicManager
already writes, so it survives a restart.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.awareness.types import Depth, SignalReading, TickResult
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
# compute_jaccard: _A_REWORDED 0.69 and _A_LOOSE 0.39 (both above the
# model-flag floor), _B 0.0 (a real change, below it).
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


# Fixtures for the overlap floor: a close and a loose rewording of _FUZZY.
_FUZZY = "The ordinary worker queue remains stable and scheduled jobs keep failing during nightly synchronization."
_FUZZY_REWORDED = "The ordinary worker queue stays stable and scheduled jobs keep failing during nightly synchronization."
_FUZZY_LOOSE = "The ordinary worker queue remains stable but backup errors require investigation."


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


_QUIET = {"scheduled_job_health": 0.0, "budget_pct_consumed": 0.1}
_RUNS = itertools.count(1)
_BETWEEN_RUNS_HOURS = 0.05
_STAMP = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def _reading(name, value):
    if isinstance(value, SignalReading):
        return value
    return SignalReading(name=name, value=value, source="t", collected_at="2026-01-01T00:00:00+00:00")


async def _run(tm, signals=None, *, gap=True):
    """Record a Light run the way production does before its summary is sent:
    the tick's stored signals and the run's corpus row. Returns ``(db, tick)``."""
    readings = [_reading(k, v) for k, v in (_QUIET if signals is None else signals).items()]
    tick = TickResult(
        tick_id=f"tick-{next(_RUNS)}",
        timestamp="2026-01-01T00:00:00+00:00",
        source="scheduled",
        signals=readings,
        scores=[],
        classified_depth=Depth.LIGHT,
        trigger_reason="test",
    )
    db = tm._db
    if db is None:
        return None, tick
    # Light runs are minutes apart (no two corpus rows within 120 s of each
    # other in production), and two runs inside the lookup window read as
    # ambiguous: let that time pass before each run (``gap=False`` for a run
    # that overlaps the one before it).
    if gap:
        await _age(db, _BETWEEN_RUNS_HOURS)
    stamp = datetime.now(UTC).strftime(_STAMP)
    await db.execute(
        "INSERT INTO awareness_ticks (id, source, signals_json, scores_json, created_at) "
        "VALUES (?, 'scheduled', ?, '[]', ?)",
        (
            tick.tick_id,
            json.dumps([
                {"name": r.name, "value": r.value, "source": "t", **({"failed": True} if r.failed else {})}
                for r in readings
            ]),
            stamp,
        ),
    )
    await db.execute(
        "INSERT INTO reflection_corpus (id, depth, prompt_text, response_text, tick_id, created_at) "
        "VALUES (?, 'Light', 'p', 'r', ?, ?)",
        (str(uuid.uuid4()), tick.tick_id, stamp),
    )
    await db.commit()
    return db, tick


async def _send(tm, assessment, *, now=None, signals=None, gap=True, **extra):
    db, tick = await _run(tm, signals, gap=gap)
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
        db=db,
        tick=tick,
    )


async def _age(db, hours: float) -> None:
    """Move every posted message, and every run's corpus row, ``hours`` into
    the past, as time passing would."""
    cur = await db.execute("SELECT id, timestamp FROM telegram_messages")
    for row_id, ts in await cur.fetchall():
        aged = datetime.fromisoformat(ts) - timedelta(hours=hours)
        await db.execute(
            "UPDATE telegram_messages SET timestamp = ? WHERE id = ?",
            (aged.isoformat(), row_id),
        )
    cur = await db.execute("SELECT id, created_at FROM reflection_corpus")
    for row_id, ts in await cur.fetchall():
        aged = datetime.fromisoformat(ts) - timedelta(hours=hours)
        await db.execute(
            "UPDATE reflection_corpus SET created_at = ? WHERE id = ?",
            (aged.strftime(_STAMP), row_id),
        )
    await db.commit()


_OVER = LIGHT_NO_CHANGE_STUB_HOURS + 0.5


async def test_absent_flag_posts_even_an_exact_repeat(db):
    tm = await _manager(db)
    assert await _send(tm, _FUZZY) == "posted"
    assert await _send(tm, _FUZZY_REWORDED) == "posted"
    assert await _send(tm, _FUZZY_REWORDED) == "posted"
    assert tm._bot.send_message.await_count == 3


async def test_near_duplicate_flagged_unchanged_is_not_posted(db):
    tm = await _manager(db)
    assert await _send(tm, _FUZZY) == "posted"
    assert await _send(tm, _FUZZY_REWORDED, material_change=False) == "skipped"
    assert tm._bot.send_message.await_count == 1


async def test_changed_summary_is_posted(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _B) == "posted"
    assert tm._bot.send_message.await_count == 2
    assert _B[:30] in tm._bot.send_message.await_args.kwargs["text"]


async def test_model_declared_no_change_is_not_posted(db):
    """``material_change: false`` from the model counts as no change while no
    condition changed, however the summary is worded: wording is not compared."""
    tm = await _manager(db)
    assert await _send(tm, _FUZZY) == "posted"
    assert await _send(tm, _FUZZY_LOOSE, material_change=False) == "skipped"
    assert await _send(tm, _B, material_change=False) == "skipped"
    assert await _send(tm, "The job is not failing.", material_change=False) == "skipped"
    assert tm._bot.send_message.await_count == 1


async def test_a_condition_change_since_the_last_post_is_posted_whatever_the_flag(db):
    """The model compares against its previous stored finding, which may not
    have been posted. Code compares this run's signals with the run behind the
    last full post, and posts when a condition changed, listing what it found."""
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    started = {**_QUIET, "scheduled_job_health": 0.5}
    assert await _send(tm, _A, material_change=False, signals=started) == "posted"
    text = tm._bot.send_message.await_args.kwargs["text"]
    assert "Changed since the last post" in text
    assert "scheduled_job_health: 0.0 → 0.5 (alarm STARTED)" in text
    # The forced post is the new baseline: the same state is quiet again, and
    # the alarm clearing is a change again.
    assert await _send(tm, _A, material_change=False, signals=started) == "skipped"
    assert await _send(tm, _A, material_change=False) == "posted"
    assert "(alarm CLEARED)" in tm._bot.send_message.await_args.kwargs["text"]
    assert tm._bot.send_message.await_count == 3


async def test_the_baseline_is_the_last_full_post_not_the_previous_run(db):
    """Runs that were not posted do not move the baseline: a value that creeps
    across a band over several unposted runs is still caught."""
    bands = {"normal_max": 0.80, "warning_threshold": 0.85, "critical_threshold": 0.90}

    def memory(value):
        return {"container_memory_pct": SignalReading(
            name="container_memory_pct", value=value, source="t",
            collected_at="2026-01-01T00:00:00+00:00", **bands,
        )}

    tm = await _manager(db)
    assert await _send(tm, _A, signals=memory(0.70)) == "posted"
    assert await _send(tm, _A, material_change=False, signals=memory(0.76)) == "skipped"
    assert await _send(tm, _A, material_change=False, signals=memory(0.79)) == "skipped"
    assert await _send(tm, _A, material_change=False, signals=memory(0.86)) == "posted"
    assert "(band normal → WARNING)" in tm._bot.send_message.await_args.kwargs["text"]


async def test_a_change_the_user_never_saw_is_posted_again(db):
    """The baseline is the last post that reached the topic, not the previous
    run: when the post announcing an alarm fails to send, the next run still
    differs from what the user last saw and posts, though nothing changed
    since the previous run."""
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    started = {**_QUIET, "scheduled_job_health": 0.5}
    working = tm._bot.send_message.side_effect
    tm._bot.send_message.side_effect = RuntimeError("synthetic send failure")
    await _send(tm, _A, material_change=False, signals=started)
    tm._bot.send_message.side_effect = working
    assert await _send(tm, _A, material_change=False, signals=started) == "posted"
    assert "(alarm STARTED)" in tm._bot.send_message.await_args.kwargs["text"]


async def test_a_post_made_while_a_collector_was_broken_is_not_a_clean_baseline(db):
    """An alarm is active and posted; a later post goes out while its collector
    is failing (stored as a placeholder zero); the collector then recovers and
    reads zero. Zero against the placeholder is not "no change": the user last
    saw the alarm active."""
    import dataclasses

    tm = await _manager(db)
    active = {**_QUIET, "scheduled_job_health": 0.5}
    assert await _send(tm, _A, signals=active) == "posted"
    broken = {**_QUIET, "scheduled_job_health": dataclasses.replace(
        _reading("scheduled_job_health", 0.0), failed=True)}
    assert await _send(tm, _B, signals=broken) == "posted"
    assert await _send(tm, _B, material_change=False, signals=broken) == "skipped"
    assert await _send(tm, _B, material_change=False) == "posted"
    assert "(reading failed in the previous run)" in tm._bot.send_message.await_args.kwargs["text"]


async def test_a_change_note_that_would_not_fit_goes_in_the_first_message(db):
    """A summary that fills one Telegram message cannot take the note at its
    end: the note would land in a second message, and if that one failed the
    first alone would become the baseline with the change never shown."""
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    db_, tick = await _run(tm, {**_QUIET, "scheduled_job_health": 0.5})
    text = "<b>Light Reflection</b>\n\n" + "word " * 808
    working = tm._bot.send_message.side_effect
    sent: list[str] = []

    async def _first_only(**kwargs):
        if sent:
            raise RuntimeError("synthetic failure of a later message")
        sent.append(kwargs["text"])
        return await working(**kwargs)

    tm._bot.send_message.side_effect = _first_only
    outcome = await send_light_topic_summary(
        "s", text, source_text=_raw("x", material_change=False), topic_manager=tm, db=db_, tick=tick,
    )
    tm._bot.send_message.side_effect = working
    assert outcome == "posted"
    assert "(alarm STARTED)" in sent[0]
    assert sent[0].startswith("<b>Light Reflection</b>\n\n<b>Changed since the last post</b>")


async def test_a_summary_already_several_messages_long_still_carries_the_note(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    db_, tick = await _run(tm, {**_QUIET, "scheduled_job_health": 0.5})
    text = "<b>Light Reflection</b>\n\n" + "word " * 1200
    assert await send_light_topic_summary(
        "s", text, source_text=_raw("x", material_change=False), topic_manager=tm, db=db_, tick=tick,
    ) == "posted"
    first = tm._bot.send_message.await_args_list[1].kwargs["text"]
    assert first.startswith("<b>Light Reflection</b>\n\n<b>Changed since the last post</b>")


async def test_a_change_note_that_fits_stays_under_the_summary(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _A, material_change=False, signals={**_QUIET, "scheduled_job_health": 0.5}) == "posted"
    text = tm._bot.send_message.await_args.kwargs["text"]
    assert text.index("Disk usage") < text.index("Changed since the last post")


async def test_drift_and_the_hidden_version_flag_never_force_a_post(db):
    tm = await _manager(db)
    assert await _send(tm, _A, signals={**_QUIET, "genesis_version_changed": 0.0}) == "posted"
    drifted = {"scheduled_job_health": 0.0, "budget_pct_consumed": 0.9, "genesis_version_changed": 1.0}
    assert await _send(tm, _A, material_change=False, signals=drifted) == "skipped"


async def test_unknown_baseline_posts(db):
    """No stored signals for the posted run, or no database or tick at all:
    the question cannot be answered, so the summary is posted."""
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    raw = _raw(_A, material_change=False)
    text = format_topic_summary(Depth.LIGHT, CCOutput(
        session_id="s", text=raw, model_used="haiku", cost_usd=0.0,
        input_tokens=1, output_tokens=1, duration_ms=1, exit_code=0,
    ))
    assert await send_light_topic_summary("s", text, source_text=raw, topic_manager=tm) == "posted"
    await db.execute("DELETE FROM awareness_ticks")
    await db.commit()
    assert await _send(tm, _A, material_change=False) == "posted"
    assert tm._bot.send_message.await_count == 3


async def test_a_long_list_of_changes_says_how_many_it_left_out(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    many = {**_QUIET, **{f"new_signal_{i:02d}": 0.5 for i in range(12)}}
    assert await _send(tm, _A, material_change=False, signals=many) == "posted"
    text = tm._bot.send_message.await_args.kwargs["text"]
    assert text.count("\n• ") == 11 and "• and 2 more" in text


async def test_parse_failure_is_never_the_baseline(db):
    tm = await _manager(db)
    await _send(tm, _FUZZY)
    bad = format_topic_summary(Depth.LIGHT, CCOutput(
        session_id="s", text="tool chatter", model_used="haiku", cost_usd=0.0,
        input_tokens=1, output_tokens=1, duration_ms=1, exit_code=0,
    ))
    await send_light_topic_summary("s", bad, source_text="tool chatter", topic_manager=tm)
    # Compared against _FUZZY (the last real summary), not the fallback.
    assert await _send(tm, _FUZZY_REWORDED, material_change=False) == "skipped"


@pytest.mark.parametrize("summary", [_FUZZY, _A])
async def test_other_thread_messages_do_not_reset_the_stub_clock(db, summary):
    from genesis.db.crud.telegram_messages import store

    tm = await _manager(db)
    await _send(tm, summary)
    await _age(db, _OVER)
    await store(
        db, chat_id=_CHAT, message_id=999_999, sender="genesis",
        content="a reply in the thread", thread_id=_THREAD, direction="outbound",
    )
    assert await _send(tm, summary, material_change=False) == "stub"


async def test_future_timestamp_counts_as_no_history(db):
    tm = await _manager(db)
    await _send(tm, _A)
    await _age(db, -2)  # clock skew: the last post looks two hours ahead
    assert await _send(tm, _A, material_change=False) == "stub"


async def test_duplicate_after_interval_posts_one_stub(db):
    tm = await _manager(db)
    assert await _send(tm, _FUZZY) == "posted"
    await _age(db, _OVER)
    assert await _send(tm, _FUZZY_REWORDED, material_change=False) == "stub"
    stub = tm._bot.send_message.await_args.kwargs["text"]
    assert "No material change since" in stub
    assert "example_sync" not in stub
    # The stub resets the liveness clock: the next duplicate is skipped.
    assert await _send(tm, _FUZZY, material_change=False) == "skipped"
    assert tm._bot.send_message.await_count == 2


async def test_duplicate_compares_against_last_full_post_not_the_stub(db):
    tm = await _manager(db)
    await _send(tm, _A)
    await _age(db, _OVER)
    assert await _send(tm, _A, material_change=False) == "stub"
    # Still the same finding a whole interval after the stub: another stub,
    # not a re-post of the full summary.
    await _age(db, _OVER)
    assert await _send(tm, _A, material_change=False) == "stub"
    assert tm._bot.send_message.await_count == 3


async def test_restart_does_not_repost_or_lose_the_stub(db):
    tm = await _manager(db)
    await _send(tm, _FUZZY)
    # A fresh manager over the same database is what a restart builds.
    tm2 = await _manager(db)
    assert await _send(tm2, _FUZZY_REWORDED, material_change=False) == "skipped"
    await _age(db, _OVER)
    tm3 = await _manager(db)
    assert await _send(tm3, _FUZZY, material_change=False) == "stub"
    tm4 = await _manager(db)
    assert await _send(tm4, _FUZZY, material_change=False) == "skipped"
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
    assert await _send(tm, words, material_change=False) == "skipped"


async def test_no_database_posts_everything(db):
    tm = TopicManager(_bot(), forum_chat_id=_CHAT)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _A) == "posted"


async def test_bridge_keeps_observation_when_post_is_skipped(db):
    """Two identical light reflections flagged unchanged: both store their
    output, one posts."""
    from genesis.cc.reflection_bridge import CCReflectionBridge

    invoker = AsyncMock()
    invoker.run = AsyncMock(
        return_value=CCOutput(
            session_id="r",
            text=_raw(_A, material_change=False),
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
    with patch(
        "genesis.cc.reflection_bridge._bridge.store_reflection_output",
        new=AsyncMock(),
    ) as store:
        for tick_id in ("t1", "t2"):
            # The awareness loop stores each tick's signals before it reflects.
            await db.execute(
                "INSERT INTO awareness_ticks (id, source, signals_json, scores_json, created_at) "
                "VALUES (?, 'scheduled', '[]', '[]', '2026-03-07T12:00:00+00:00')",
                (tick_id,),
            )
            await db.commit()
            tick = TickResult(
                tick_id=tick_id,
                timestamp="2026-03-07T12:00:00",
                source="scheduled",
                signals=[],
                scores=[],
                classified_depth=Depth.LIGHT,
                trigger_reason="test",
            )
            result = await bridge.reflect(Depth.LIGHT, tick, db=db)
            assert result.success, result.reason
    assert store.await_count == 2
    assert tm._bot.send_message.await_count == 1


async def test_duplicate_inside_interval_stays_silent(db):
    tm = await _manager(db)
    await _send(tm, _A)
    await _age(db, LIGHT_NO_CHANGE_STUB_HOURS - 0.5)
    assert await _send(tm, _A, material_change=False) == "skipped"


async def test_material_change_true_is_never_suppressed(db):
    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, _A_REWORDED, material_change=True) == "posted"
    assert tm._bot.send_message.await_count == 2


def _long_summary(tail):
    return "<b>Light Reflection</b>\n\n" + "alpha beta gamma delta " * 230 + tail


async def _send_formatted(tm, text):
    db, tick = await _run(tm)
    return await send_light_topic_summary(
        "s", text, source_text='{"material_change": false}', topic_manager=tm, db=db, tick=tick,
    )


async def test_change_in_a_later_chunk_is_not_suppressed(db):
    from genesis.channels.telegram._handler_helpers import _split_for_telegram

    tm = await _manager(db)
    before, after = _long_summary("old state"), _long_summary("new state")
    old_chunks, new_chunks = _split_for_telegram(before), _split_for_telegram(after)
    assert len(old_chunks) > 1 and old_chunks[0] == new_chunks[0]
    assert old_chunks[-1] != new_chunks[-1]
    assert await _send_formatted(tm, before) == "posted"
    assert await _send_formatted(tm, after) == "posted"
    assert tm._bot.send_message.await_count == len(old_chunks) + len(new_chunks)


async def test_exact_whole_multichunk_repeat_is_suppressed_after_restart(db):
    tm = await _manager(db)
    before = _long_summary("same state")
    assert await _send_formatted(tm, before) == "posted"
    restarted = await _manager(db)
    assert await _send_formatted(restarted, before) == "skipped"
    assert restarted._bot.send_message.await_count == 0


async def test_partial_previous_delivery_cannot_hide_the_new_tail(db):
    tm = await _manager(db)
    before = _long_summary("old state")
    first = MagicMock()
    first.message_id = next(_IDS)
    tm._bot.send_message.side_effect = [first, RuntimeError("synthetic second-chunk failure")]
    assert await _send_formatted(tm, before) == "posted"
    restarted = await _manager(db)
    assert await _send_formatted(restarted, _long_summary("new state")) == "posted"


async def test_future_dated_last_post_does_not_block_the_stub(db):
    from datetime import UTC

    tm = await _manager(db)
    assert await _send(tm, _A) == "posted"
    await _age(db, -1)
    assert await _send(tm, _A, material_change=False) == "stub"
    rows = await tm.recent_outbound("reflection_light", limit=20)
    assert "No material change since" in rows[0]["content"]
    assert datetime.fromisoformat(rows[0]["timestamp"]) <= datetime.now(UTC)
    restarted = await _manager(db)
    assert await _send(restarted, _A, material_change=False) == "skipped"


async def test_fallback_with_trailing_reply_never_becomes_baseline(db):
    from genesis.db.crud.telegram_messages import store

    tm = await _manager(db)
    await _send(tm, _FUZZY)
    fallback = format_topic_summary(Depth.LIGHT, CCOutput(
        session_id="s", text="synthetic unparseable output", model_used="haiku", cost_usd=0.0,
        input_tokens=1, output_tokens=1, duration_ms=1, exit_code=0,
    ))
    await tm.send_to_category("reflection_light", fallback)
    await store(db, chat_id=_CHAT, message_id=999_998, sender="genesis",
                content="a reply", thread_id=_THREAD, direction="outbound")
    assert await _send(tm, _FUZZY_REWORDED, material_change=False) == "skipped"


async def test_window_without_the_first_chunk_posts(db):
    tm = await _manager(db)
    tm.recent_outbound = AsyncMock(return_value=[{"content": "continuation only", "timestamp": "2026-01-01"}])
    assert await _send(tm, _A, material_change=False) == "posted"


async def test_flagged_unchanged_with_no_history_posts(db):
    tm = await _manager(db)
    assert await _send(tm, _A, material_change=False) == "posted"
    tm.recent_outbound = AsyncMock(side_effect=RuntimeError("synthetic read failure"))
    assert await _send(tm, _A, material_change=False) == "posted"
    assert tm._bot.send_message.await_count == 2


async def test_parsed_assessment_ending_in_fallback_words_remains_the_baseline(db):
    tm = await _manager(db)
    real_update = "New upstream deployment detected; rollout details stored for review."
    assert await _send(tm, _A) == "posted"
    assert await _send(tm, real_update) == "posted"
    assert await _send(tm, real_update, material_change=False) == "skipped"
    assert await _send(tm, _A) == "posted"


@pytest.mark.parametrize(("before", "after"), [
    (_A, _A_REWORDED), (_A, _A_LOOSE),
    ("queue depth 0", "queue depth 9"),
    ("job health=0.0", "job health=1.0"),
    ("job health=0.5", "job health=-0.5"),
    ("job health=０", "job health=１"),
    ("latency 1ms", "latency 1s"),
    ("worker queue stable", "worker queue stable depth=1"),
])
@pytest.mark.parametrize("expired", [False, True])
async def test_absent_flag_posts_every_change(db, before, after, expired):
    tm = await _manager(db)
    assert await _send(tm, before) == "posted"
    if expired:
        await _age(db, _OVER)
    assert await _send(tm, after) == "posted"
    assert tm._bot.send_message.await_count == 2


@pytest.mark.parametrize(("before", "after"), [
    (_A, _A_REWORDED),
    ("queue depth 0", "queue depth 9"),
    ("job health=0.0", "job health=1.0"),
])
async def test_model_flag_is_trusted_over_numeric_drift(db, before, after):
    """No changed-number rule: whether a moved value is material is the
    model's call, so a false flag suppresses numeric drift in the text."""
    tm = await _manager(db)
    assert await _send(tm, before) == "posted"
    assert await _send(tm, after, material_change=False) == "skipped"
    assert tm._bot.send_message.await_count == 1


@pytest.mark.parametrize("text", ["0", "0.0", "-1", "０", _A])
async def test_exact_numeric_repeat_survives_restart_and_stub(db, text):
    tm = await _manager(db)
    assert await _send(tm, text) == "posted"
    restarted = await _manager(db)
    assert await _send(restarted, text, material_change=False) == "skipped"
    await _age(db, _OVER)
    assert await _send(restarted, text, material_change=False) == "stub"
    assert await _send(restarted, text, material_change=False) == "skipped"


@pytest.mark.parametrize("existing", [False, True])
async def test_concurrent_light_deliveries_share_one_history_decision(db, existing):
    tm = await _manager(db)
    if existing:
        await _send(tm, _A, material_change=False)
        await _age(db, _OVER)
    first_read, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = tm.recent_outbound
    reads = 0

    async def history(*args, **kwargs):
        nonlocal reads
        reads += 1
        rows = await original(*args, **kwargs)
        if reads == 1:
            first_read.set()
            await release.wait()
        return rows

    tm.recent_outbound = history
    first = asyncio.create_task(_send(tm, _A, material_change=False))
    await asyncio.wait_for(first_read.wait(), 2)

    async def second_send():
        second_started.set()
        return await _send(tm, _A, material_change=False, gap=False)

    second = asyncio.create_task(second_send())
    try:
        await asyncio.wait_for(second_started.wait(), 2)
        assert reads == 1  # The waiter must not read a stale baseline.
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert results == ["stub" if existing else "posted", "skipped"]
    assert tm._bot.send_message.await_count == (2 if existing else 1)


async def test_delivery_lock_is_stable_and_waiter_cancellation_keeps_holder(db):
    tm = await _manager(db)
    lock = tm.delivery_lock("reflection_light")
    assert lock is tm.delivery_lock("reflection_light")
    assert lock is not tm.delivery_lock("reflection_deep")
    started = asyncio.Event()

    async def waiting_send():
        started.set()
        return await _send(tm, _A)

    async with lock:
        waiter = asyncio.create_task(waiting_send())
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert not waiter.done()
            assert await asyncio.wait_for(tm.send_to_category("reflection_deep", "other topic"), 2)
        finally:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert lock.locked()
    assert not lock.locked()
    assert await asyncio.wait_for(_send(tm, _A), 2) == "posted"


@pytest.mark.parametrize("fault", ["read", "send", "cancel"])
async def test_delivery_lock_released_after_failed_or_cancelled_work(db, fault):
    tm = await _manager(db)
    lock = tm.delivery_lock("reflection_light")
    read, send = tm.recent_outbound, tm._bot.send_message.side_effect
    if fault == "read":
        tm.recent_outbound = AsyncMock(side_effect=RuntimeError("synthetic read failure"))
    else:
        error = asyncio.CancelledError() if fault == "cancel" else RuntimeError("synthetic send failure")
        tm._bot.send_message.side_effect = error
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await _send(tm, _A, material_change=False)
    else:
        assert await _send(tm, _A, material_change=False) == "posted"
    assert not lock.locked()
    tm.recent_outbound, tm._bot.send_message.side_effect = read, send
    assert await asyncio.wait_for(_send(tm, _A, material_change=False), 2) == ("skipped" if fault == "read" else "posted")
    assert await _send(tm, _A, material_change=False) == "skipped"
