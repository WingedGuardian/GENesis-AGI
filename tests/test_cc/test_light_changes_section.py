"""The Light prompt's change table compares against the run behind the previous
finding (#4371): that run is found by its corpus row, written just before the
finding, never by some other Light run."""

import json
import uuid

import pytest

from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.cc.reflection_bridge._prompts import _light_changes_section


def _tick(tick_id="tick-now", **values):
    return TickResult(
        tick_id=tick_id,
        timestamp="2026-11-11T12:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(name=k, value=v, source="t", collected_at="2026-11-11T12:00:00+00:00")
            for k, v in values.items()
        ],
        scores=[],
        classified_depth=Depth.LIGHT,
        trigger_reason="test",
    )


async def _stored_tick(db, tick_id, **values):
    await db.execute(
        "INSERT INTO awareness_ticks (id, source, signals_json, scores_json, created_at) "
        "VALUES (?, 'scheduled', ?, '[]', '2026-11-11T09:00:00+00:00')",
        (tick_id, json.dumps([{"name": k, "value": v, "source": "t"} for k, v in values.items()])),
    )


async def _corpus(db, tick_id, created_at):
    await db.execute(
        "INSERT INTO reflection_corpus (id, depth, prompt_text, response_text, tick_id, created_at) "
        "VALUES (?, 'Light', 'p', 'r', ?, ?)",
        (str(uuid.uuid4()), tick_id, created_at),
    )
    await db.commit()


FINDING_AT = "2026-11-11T09:00:05.300000+00:00"


@pytest.mark.asyncio
async def test_the_table_diffs_against_the_run_behind_the_finding(db):
    await _stored_tick(db, "tick-prev", sentinel_activity=0.5, budget_pct_consumed=0.1)
    await _corpus(db, "tick-prev", "2026-11-11T09:00:05.100000+00:00")
    section = await _light_changes_section(
        db, _tick(sentinel_activity=0.0, budget_pct_consumed=0.1), FINDING_AT
    )
    assert "## Changes since the previous Light finding" in section
    assert "sentinel_activity: 0.5 → 0.0 (alarm CLEARED)" in section
    assert "1 other signal(s) unchanged." in section


@pytest.mark.asyncio
async def test_a_newer_run_without_a_finding_is_never_the_baseline(db):
    """A run whose output failed to parse writes a corpus row after the finding
    and no finding of its own: the table must still compare against the run
    that wrote the finding shown."""
    await _stored_tick(db, "tick-prev", sentinel_activity=0.5)
    await _stored_tick(db, "tick-failed", sentinel_activity=0.0)
    await _corpus(db, "tick-prev", "2026-11-11T09:00:05.100000+00:00")
    await _corpus(db, "tick-failed", "2026-11-11T09:30:00.000000+00:00")
    section = await _light_changes_section(db, _tick(sentinel_activity=0.0), FINDING_AT)
    assert "sentinel_activity: 0.5 → 0.0 (alarm CLEARED)" in section


@pytest.mark.asyncio
async def test_the_current_tick_is_never_its_own_baseline(db):
    """The deferred-retry path reflects with the current tick, whose corpus row
    could fall inside the window."""
    await _stored_tick(db, "tick-now", sentinel_activity=0.0)
    await _corpus(db, "tick-now", "2026-11-11T09:00:05.200000+00:00")
    section = await _light_changes_section(db, _tick(sentinel_activity=0.0), FINDING_AT)
    assert "The previous run's signals are not on record." in section


@pytest.mark.asyncio
async def test_no_finding_or_no_matching_run_says_so(db):
    none = await _light_changes_section(db, _tick(sentinel_activity=0.0), None)
    assert none.endswith("No previous Light finding on record.")
    await _stored_tick(db, "tick-old", sentinel_activity=0.5)
    await _corpus(db, "tick-old", "2026-11-11T08:00:00.000000+00:00")  # outside the window
    missing = await _light_changes_section(db, _tick(sentinel_activity=0.0), FINDING_AT)
    assert missing.endswith("The previous run's signals are not on record.")


@pytest.mark.asyncio
async def test_two_runs_in_the_window_have_no_baseline(db):
    """Overlapping reflections can write their corpus rows in either order
    around one's output, so the newest row is not necessarily the run behind
    it. Two candidates is no answer."""
    from genesis.cc.reflection_bridge._prompts import _signals_behind
    from genesis.db.crud import reflection_corpus

    await _stored_tick(db, "tick-a", critical_failure=0.0)
    await _stored_tick(db, "tick-b", critical_failure=1.0)
    await _corpus(db, "tick-a", "2026-11-11T09:00:00.000000+00:00")
    assert await reflection_corpus.tick_written_before(db, "Light", FINDING_AT) == "tick-a"
    await _corpus(db, "tick-b", "2026-11-11T09:00:01.000000+00:00")
    assert await reflection_corpus.tick_written_before(db, "Light", FINDING_AT) is None
    assert await _signals_behind(db, FINDING_AT, exclude_tick_id=None) is None
    # The excluded run is not a candidate: one remains.
    assert await reflection_corpus.tick_written_before(
        db, "Light", FINDING_AT, exclude_tick_id="tick-b"
    ) == "tick-a"


@pytest.mark.asyncio
async def test_a_failed_reading_in_the_stored_run_loads_as_unknown(db):
    from genesis.cc.reflection_bridge._prompts import _signals_behind

    await db.execute(
        "INSERT INTO awareness_ticks (id, source, signals_json, scores_json, created_at) "
        "VALUES ('tick-f', 'scheduled', ?, '[]', '2026-11-11T09:00:00+00:00')",
        (json.dumps([
            {"name": "scheduled_job_health", "value": 0.0, "source": "t", "failed": True},
            {"name": "budget_pct_consumed", "value": 0.2, "source": "t"},
        ]),),
    )
    await _corpus(db, "tick-f", "2026-11-11T09:00:00.000000+00:00")
    assert await _signals_behind(db, FINDING_AT, exclude_tick_id=None) == {
        "scheduled_job_health": None,
        "budget_pct_consumed": 0.2,
    }
