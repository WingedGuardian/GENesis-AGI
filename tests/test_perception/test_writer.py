"""Tests for ResultWriter — stores observations, emits events."""

from __future__ import annotations

import json

from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.perception.types import LightOutput, MicroOutput, UserModelDelta


def _make_tick() -> TickResult:
    return TickResult(
        tick_id="tick-1",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name="cpu_usage", value=0.3, source="system",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
        ],
        scores=[],
        classified_depth=Depth.MICRO,
        trigger_reason="threshold_exceeded",
    )


async def test_write_micro_creates_observation(db):
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["idle", "resource_normal"],
        salience=0.6,
        anomaly=False,
        summary="All systems normal.",
        signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert "idle" in obs[0]["content"]
    assert obs[0]["type"] == "micro_reflection"


async def test_write_micro_anomaly_tags_observation(db):
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["anomaly_detected"],
        salience=0.8,
        anomaly=True,
        summary="CPU spike detected.",
        signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["category"] == "anomaly"


async def test_write_light_creates_observation(db):
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = LightOutput(
        assessment="System is idle.",
        patterns=["declining_activity"],
        user_model_updates=[],
        recommendations=["Schedule maintenance"],
        confidence=0.7,
        focus_area="situation",
    )
    await writer.write(output, Depth.LIGHT, _make_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["type"] == "light_reflection"
    assert "idle" in obs[0]["content"]


async def test_write_light_with_user_model_deltas_stored_as_observations(db):
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = LightOutput(
        assessment="User appears to prefer EST timezone.",
        patterns=[],
        user_model_updates=[
            UserModelDelta(
                field="timezone", value="EST",
                evidence="user mentioned EST", confidence=0.9,
            ),
        ],
        recommendations=[],
        confidence=0.8,
        focus_area="user_impact",
    )
    await writer.write(output, Depth.LIGHT, _make_tick(), db=db)

    # Light reflection observation + user model delta observation
    obs = await observations.query(db, source="reflection")
    assert len(obs) >= 1
    delta_obs = await observations.query(db, type="user_model_delta")
    assert len(delta_obs) == 1
    assert "timezone" in delta_obs[0]["content"]


async def test_write_micro_with_memory_store(db):
    from unittest.mock import AsyncMock

    from genesis.perception.writer import ResultWriter

    store = AsyncMock()
    writer = ResultWriter(memory_store=store)
    output = MicroOutput(
        tags=["idle", "resource_normal"],
        salience=0.6,
        anomaly=False,
        summary="All systems normal.",
        signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    store.store.assert_awaited_once_with(
        "All systems normal.",
        "reflection",
        memory_type="episodic",
        tags=["idle", "resource_normal"],
        confidence=0.6,
        source_pipeline="reflection",
    )


async def test_write_light_with_memory_store(db):
    from unittest.mock import AsyncMock

    from genesis.perception.writer import ResultWriter

    store = AsyncMock()
    writer = ResultWriter(memory_store=store)
    output = LightOutput(
        assessment="System is idle.",
        patterns=["declining_activity"],
        user_model_updates=[],
        recommendations=["Schedule maintenance"],
        confidence=0.7,
        focus_area="situation",
    )
    await writer.write(output, Depth.LIGHT, _make_tick(), db=db)

    store.store.assert_awaited_once_with(
        "System is idle.",
        "reflection",
        memory_type="episodic",
        tags=["declining_activity"],
        confidence=0.7,
        source_pipeline="reflection",
    )


async def test_write_emits_event(db):
    from genesis.observability.events import GenesisEventBus
    from genesis.perception.writer import ResultWriter

    bus = GenesisEventBus()
    events = []

    async def listener(e):
        events.append(e)

    bus.subscribe(listener)

    writer = ResultWriter(event_bus=bus)
    output = MicroOutput(
        tags=["idle"], salience=0.5, anomaly=False,
        summary="Normal.", signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    assert len(events) == 1
    assert events[0].event_type == "reflection.completed"


# ── Content-hash dedup tests ──────────────────────────────────────────────


async def test_micro_dedup_skips_duplicate(db):
    """Second micro write with identical content is skipped."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["stable"], salience=0.5, anomaly=False,
        summary="All systems nominal.", signals_examined=5,
    )
    tick = _make_tick()

    await writer.write(output, Depth.MICRO, tick, db=db)
    await writer.write(output, Depth.MICRO, tick, db=db)  # duplicate

    rows = await observations.query(db, source="reflection", type="micro_reflection")
    assert len(rows) == 1


async def test_micro_stores_novel_content(db):
    """Different content creates separate observations."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    tick = _make_tick()
    output1 = MicroOutput(
        tags=["stable"], salience=0.5, anomaly=False,
        summary="All systems nominal.", signals_examined=5,
    )
    output2 = MicroOutput(
        tags=["alert"], salience=0.8, anomaly=True,
        summary="Memory spike detected.", signals_examined=5,
    )

    await writer.write(output1, Depth.MICRO, tick, db=db)
    await writer.write(output2, Depth.MICRO, tick, db=db)

    rows = await observations.query(db, source="reflection", type="micro_reflection")
    assert len(rows) == 2


async def test_light_dedup_skips_duplicate(db):
    """Second light write with identical content is skipped."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    tick = _make_tick()
    output = LightOutput(
        assessment="System is idle.",
        patterns=["declining_activity"],
        user_model_updates=[],
        recommendations=["Schedule maintenance"],
        confidence=0.7,
        focus_area="situation",
    )

    await writer.write(output, Depth.LIGHT, tick, db=db)
    await writer.write(output, Depth.LIGHT, tick, db=db)  # duplicate

    rows = await observations.query(db, source="reflection", type="light_reflection")
    assert len(rows) == 1


async def test_surplus_candidate_dedup_via_upsert(db):
    """Duplicate surplus candidates are idempotent via surplus_insights upsert."""
    from genesis.db.crud import surplus as surplus_crud
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    tick = _make_tick()
    output = LightOutput(
        assessment="Assessment.",
        patterns=[],
        user_model_updates=[],
        recommendations=[],
        confidence=0.5,
        focus_area="situation",
        surplus_candidates=["Explore caching strategy"],
    )

    await writer.write(output, Depth.LIGHT, tick, db=db)
    await writer.write(output, Depth.LIGHT, tick, db=db)  # duplicate — upsert, not insert

    rows = await surplus_crud.list_pending(db)
    assert len(rows) == 1
    assert rows[0]["content"] == "Explore caching strategy"


async def test_user_model_delta_dedup(db):
    """Duplicate user model deltas are skipped."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    tick = _make_tick()
    output = LightOutput(
        assessment="User prefers EST.",
        patterns=[],
        user_model_updates=[
            UserModelDelta(
                field="timezone", value="EST",
                evidence="user mentioned EST", confidence=0.9,
            ),
        ],
        recommendations=[],
        confidence=0.8,
        focus_area="user_impact",
    )

    await writer.write(output, Depth.LIGHT, tick, db=db)
    await writer.write(output, Depth.LIGHT, tick, db=db)  # duplicate

    rows = await observations.query(db, type="user_model_delta")
    assert len(rows) == 1


# ── Confidence gate tests ─────────────────────────────────────────────────


async def test_delta_confidence_gate(db):
    """Deltas below MIN_DELTA_CONFIDENCE (0.90) are filtered out."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    tick = _make_tick()
    output = LightOutput(
        assessment="User analysis.",
        patterns=[],
        user_model_updates=[
            UserModelDelta(field="role", value="engineer", evidence="mentioned", confidence=0.92),
            UserModelDelta(field="lang", value="Python", evidence="guessed", confidence=0.85),
            UserModelDelta(field="os", value="Linux", evidence="weak", confidence=0.89),
        ],
        recommendations=[],
        confidence=0.8,
        focus_area="user_impact",
    )

    await writer.write(output, Depth.LIGHT, tick, db=db)

    rows = await observations.query(db, type="user_model_delta")
    assert len(rows) == 1
    content = json.loads(rows[0]["content"])
    assert content["field"] == "role"
    assert content["confidence"] == 0.92


async def test_delta_field_value_dedup(db):
    """Same (field, value) with different evidence is caught by dedup."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()

    output1 = LightOutput(
        assessment="First.", patterns=[], recommendations=[],
        user_model_updates=[
            UserModelDelta(field="tz", value="EST", evidence="said it once", confidence=0.95),
        ],
        confidence=0.8, focus_area="user_impact",
    )
    output2 = LightOutput(
        assessment="Second.", patterns=[], recommendations=[],
        user_model_updates=[
            UserModelDelta(field="tz", value="EST", evidence="said it differently", confidence=0.92),
        ],
        confidence=0.8, focus_area="user_impact",
    )

    await writer.write(output1, Depth.LIGHT, _make_tick(), db=db)
    await writer.write(output2, Depth.LIGHT, _make_tick(), db=db)

    rows = await observations.query(db, type="user_model_delta")
    assert len(rows) == 1, f"Expected dedup to catch duplicate (field,value), got {len(rows)}"


# ── Salience gate tests ───────────────────────────────────────────────────


async def test_micro_skips_low_salience(db):
    """Salience below 0.1 and non-anomaly = skip."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=[], salience=0.05, anomaly=False,
        summary="Nothing noteworthy.", signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    rows = await observations.query(db, source="reflection", type="micro_reflection")
    assert len(rows) == 0


async def test_micro_keeps_low_salience_anomaly(db):
    """Anomaly with low salience is still stored (safety guard)."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["anomaly"], salience=0.05, anomaly=True,
        summary="Unusual pattern at low confidence.", signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    rows = await observations.query(db, source="reflection", type="micro_reflection")
    assert len(rows) == 1


async def test_micro_boundary_salience_stored(db):
    """Salience exactly at 0.45 is stored (gate is strictly less-than)."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["borderline"], salience=0.45, anomaly=False,
        summary="Borderline observation.", signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    rows = await observations.query(db, source="reflection", type="micro_reflection")
    assert len(rows) == 1


# ── Normalized dedup tests ───────────────────────────────────────────────


def test_normalize_for_dedup_strips_numbers():
    """Numeric variation should be collapsed."""
    from genesis.perception.writer import ResultWriter

    n = ResultWriter._normalize_for_dedup
    assert n("memory at 78% is fine") == n("memory at 79% is fine")
    assert n("CPU 0.45 stable") == n("CPU 0.83 stable")
    assert n("3 of 5 signals healthy") == n("2 of 5 signals healthy")


def test_normalize_for_dedup_preserves_structure():
    """Structurally different summaries should NOT collapse."""
    from genesis.perception.writer import ResultWriter

    n = ResultWriter._normalize_for_dedup
    assert n("memory is fine") != n("cpu is fine")
    assert n("all systems nominal") != n("anomaly detected in network")


async def test_micro_normalized_dedup_catches_near_duplicate(db):
    """Near-identical summaries differing only in numbers should dedup."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    tick = _make_tick()
    output1 = MicroOutput(
        tags=["stable"], salience=0.6, anomaly=False,
        summary="Memory usage at 78% is within normal range.",
        signals_examined=5,
    )
    output2 = MicroOutput(
        tags=["stable"], salience=0.62, anomaly=False,
        summary="Memory usage at 79% is within normal range.",
        signals_examined=5,
    )

    await writer.write(output1, Depth.MICRO, tick, db=db)
    stored = await writer.write(output2, Depth.MICRO, tick, db=db)

    rows = await observations.query(db, source="reflection", type="micro_reflection")
    assert len(rows) == 1, "Near-duplicate should be caught by normalized hash"
    assert not stored


async def test_micro_cooldown_blocks_rapid_non_anomaly(db):
    """Non-anomaly micro within 20 min of a recent one should be blocked."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()

    # Insert a recent micro_reflection with current timestamp
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-micro",
        source="reflection",
        type="micro_reflection",
        content='{"summary": "prior"}',
        priority="low",
        created_at=now,
    )

    output = MicroOutput(
        tags=["stable"], salience=0.6, anomaly=False,
        summary="Different wording but same state.",
        signals_examined=5,
    )
    stored = await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    assert not stored, "Cooldown should block non-anomaly within 20 min"


async def test_micro_cooldown_allows_anomaly(db):
    """Anomaly bypasses cooldown even if recent micro exists."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()

    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-micro",
        source="reflection",
        type="micro_reflection",
        content='{"summary": "prior"}',
        priority="low",
        created_at=now,
    )

    output = MicroOutput(
        tags=["anomaly"], salience=0.8, anomaly=True,
        summary="Critical failure detected.",
        signals_examined=5,
    )
    stored = await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    assert stored, "Anomaly should bypass cooldown"
    rows = await observations.query(db, source="reflection", type="micro_reflection")
    assert len(rows) == 2  # both the prior and the anomaly
