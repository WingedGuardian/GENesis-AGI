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


def _make_mixed_tick() -> TickResult:
    """Tick whose roster mixes a user-facing and a genesis-infra signal."""
    return TickResult(
        tick_id="tick-mixed",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name="task_completion_quality", value=0.9, source="genesis",
                collected_at="2026-03-05T10:00:00+00:00",
            ),
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
        summary="Memory usage elevated at 82% on primary node.",
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
    assert obs[0]["category"] == "anomaly:genesis"


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
        summary="Memory usage elevated at 82% on primary node.",
        signals_examined=5,
    )
    await writer.write(output, Depth.MICRO, _make_tick(), db=db)

    store.store.assert_awaited_once_with(
        "Memory usage elevated at 82% on primary node.",
        "reflection",
        memory_type="episodic",
        tags=["idle", "resource_normal"],
        confidence=0.6,
        source_pipeline="reflection",
        source_subsystem="reflection",
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
        source_subsystem="reflection",
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


# ── Structural dedup tests ────────────────────────────────────────────────


async def test_micro_structural_dedup_catches_near_duplicate(db):
    """Same tags + salience band + anomaly should dedup regardless of summary text."""
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

    # Insert a recent micro_reflection with current timestamp.  _make_tick()
    # (cpu_usage only) classifies as :genesis, so seed a matching category.
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-micro",
        source="reflection",
        type="micro_reflection",
        category="routine:genesis",
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

    assert not stored, "Cooldown should block same-relevance non-anomaly within 20 min"


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


async def test_write_micro_dedup_same_structure(db):
    """Two micro outputs with same tags/anomaly/signals but different summaries should dedup."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output1 = MicroOutput(
        tags=["anomaly_detected", "staleness"],
        salience=0.7,
        anomaly=True,
        summary="Significant anomaly in user_goal_staleness signal at 1.0",
        signals_examined=21,
    )
    output2 = MicroOutput(
        tags=["anomaly_detected", "staleness"],
        salience=0.7,
        anomaly=True,
        summary="The combination of signals indicates user_goal_staleness anomaly",
        signals_examined=22,
    )
    tick = _make_tick()
    await writer.write(output1, Depth.MICRO, tick, db=db)
    await writer.write(output2, Depth.MICRO, tick, db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1, f"Expected 1 observation (dedup), got {len(obs)}"


async def test_write_micro_different_structure_not_deduped(db):
    """Micro outputs with different tags should NOT dedup."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output1 = MicroOutput(
        tags=["anomaly_detected"],
        salience=0.7,
        anomaly=True,
        summary="CPU spike detected.",
        signals_examined=5,
    )
    output2 = MicroOutput(
        tags=["idle", "resource_normal"],
        salience=0.6,
        anomaly=False,
        summary="Memory usage elevated at 82% on primary node.",
        signals_examined=5,
    )
    tick = _make_tick()
    await writer.write(output1, Depth.MICRO, tick, db=db)
    await writer.write(output2, Depth.MICRO, tick, db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 2, f"Expected 2 observations (different structure), got {len(obs)}"


async def test_micro_low_info_gate_rejects(db):
    """Low-information micro observation is gated."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["idle"],
        salience=0.7,
        anomaly=False,
        summary="No significant changes detected in system state.",
        signals_examined=3,
    )
    stored = await writer.write(output, Depth.MICRO, _make_tick(), db=db)
    assert stored is False
    obs = await observations.query(db, source="reflection")
    assert len(obs) == 0


async def test_micro_low_info_gate_passes_real_content(db):
    """Informative micro observation passes the low-info gate."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["memory", "elevated"],
        salience=0.7,
        anomaly=False,
        summary="Memory backlog at 92% for 18 hours — needs investigation.",
        signals_examined=5,
    )
    stored = await writer.write(output, Depth.MICRO, _make_tick(), db=db)
    assert stored is True
    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1


async def test_write_micro_dedup_ignores_tag_variation(db):
    """Two micro outputs with DIFFERENT tags but same signals/salience/anomaly should dedup.

    LLM-generated tags vary wildly across ticks (61 distinct combos for 72
    observations in production).  Tags must not be part of the dedup hash.
    """
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output1 = MicroOutput(
        tags=["user_engagement_spike", "strategic_reflection_overdue"],
        salience=0.7,
        anomaly=True,
        summary="User session patterns show an unusual spike.",
        signals_examined=21,
    )
    output2 = MicroOutput(
        tags=["cross_signal_analysis", "awareness_cycle"],
        salience=0.7,
        anomaly=True,
        summary="The awareness cycle shows a potential imbalance.",
        signals_examined=21,
    )
    tick = _make_tick()
    await writer.write(output1, Depth.MICRO, tick, db=db)
    await writer.write(output2, Depth.MICRO, tick, db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1, f"Expected 1 (tag variation should dedup), got {len(obs)}"


# ── Relevance from driving_signals (idx 17) ─────────────────────────────


async def test_write_micro_relevance_user_from_driving_signals(db):
    """Mixed roster, but the LLM says only the user signal drove it → :user."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["user_activity"],
        salience=0.7,
        anomaly=False,
        summary="Task completion quality shifted noticeably.",
        signals_examined=2,
        driving_signals=["task_completion_quality"],
    )
    await writer.write(output, Depth.MICRO, _make_mixed_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["category"] == "routine:user"


async def test_write_micro_relevance_genesis_from_driving_signals(db):
    """Mixed roster, driving signal is genesis-infra → :genesis."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["cpu"],
        salience=0.7,
        anomaly=False,
        summary="CPU trending up across ticks.",
        signals_examined=2,
        driving_signals=["cpu_usage"],
    )
    await writer.write(output, Depth.MICRO, _make_mixed_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["category"] == "routine:genesis"


async def test_write_micro_relevance_mixed_driving_signals_both(db):
    """Driving signals span both audiences → :both."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["mixed"],
        salience=0.7,
        anomaly=False,
        summary="User activity and CPU both moved.",
        signals_examined=2,
        driving_signals=["task_completion_quality", "cpu_usage"],
    )
    await writer.write(output, Depth.MICRO, _make_mixed_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["category"] == "routine:both"


async def test_write_micro_relevance_empty_driving_signals_falls_back(db):
    """LLM omitted driving_signals → full-roster fallback (pre-fix behavior)."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["idle"],
        salience=0.7,
        anomaly=False,
        summary="Broad routine sweep.",
        signals_examined=2,
    )
    await writer.write(output, Depth.MICRO, _make_mixed_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["category"] == "routine:both"


async def test_write_micro_relevance_hallucinated_driving_signals_fall_back(db):
    """Names not in the tick roster are discarded → full-roster fallback."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    output = MicroOutput(
        tags=["ghost"],
        salience=0.7,
        anomaly=False,
        summary="Phantom signal cited.",
        signals_examined=2,
        driving_signals=["signal_that_does_not_exist"],
    )
    await writer.write(output, Depth.MICRO, _make_mixed_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1
    assert obs[0]["category"] == "routine:both"


async def test_write_micro_dedup_distinguishes_relevance(db):
    """Same roster/band/anomaly but different driving_signals -> different
    relevance -> both persist (dedup must not collapse distinct partitions)."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    # anomaly=True bypasses the cooldown gate, isolating the dedup path.
    user_output = MicroOutput(
        tags=["user"],
        salience=0.8,
        anomaly=True,
        summary="Task completion quality anomaly.",
        signals_examined=2,
        driving_signals=["task_completion_quality"],
    )
    genesis_output = MicroOutput(
        tags=["genesis"],
        salience=0.8,
        anomaly=True,
        summary="CPU usage anomaly.",
        signals_examined=2,
        driving_signals=["cpu_usage"],
    )
    stored_user = await writer.write(user_output, Depth.MICRO, _make_mixed_tick(), db=db)
    stored_genesis = await writer.write(genesis_output, Depth.MICRO, _make_mixed_tick(), db=db)

    assert stored_user is True
    assert stored_genesis is True
    obs = await observations.query(db, source="reflection")
    cats = sorted(o["category"] for o in obs)
    assert cats == ["anomaly:genesis", "anomaly:user"]


async def test_micro_cooldown_scoped_by_relevance(db):
    """A recent :user micro must NOT block a :genesis micro (different ego
    partition), but a recent same-relevance micro still hits the cooldown."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-user-micro",
        source="reflection",
        type="micro_reflection",
        category="routine:user",
        content='{"summary": "prior user-relevant"}',
        priority="low",
        created_at=now,
    )

    # genesis-driven micro on a mixed roster -> routine:genesis; the recent
    # :user micro is a different partition and must not suppress it.
    genesis_output = MicroOutput(
        tags=["cpu"], salience=0.6, anomaly=False,
        summary="Disk usage creeping upward.",
        signals_examined=2,
        driving_signals=["cpu_usage"],
    )
    stored_genesis = await writer.write(
        genesis_output, Depth.MICRO, _make_mixed_tick(), db=db)
    assert stored_genesis is True, "cross-relevance cooldown must not block"

    # a recent same-relevance (:genesis) micro DOES suppress the next one.
    await observations.create(
        db,
        id="recent-genesis-micro",
        source="reflection",
        type="micro_reflection",
        category="routine:genesis",
        content='{"summary": "prior genesis-relevant"}',
        priority="low",
        created_at=now,
    )
    genesis_output2 = MicroOutput(
        tags=["cpu2"], salience=0.7, anomaly=False,
        summary="Disk usage still creeping.",
        signals_examined=2,
        driving_signals=["cpu_usage"],
    )
    stored_genesis2 = await writer.write(
        genesis_output2, Depth.MICRO, _make_mixed_tick(), db=db)
    assert stored_genesis2 is False, "same-relevance cooldown must still block"


async def test_micro_cooldown_both_suppresses_genesis(db):
    """A recent :both micro shares the Genesis-visible partition, so it must
    suppress a subsequent :genesis micro (P2: :both overlaps :genesis)."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-both-micro",
        source="reflection",
        type="micro_reflection",
        category="routine:both",
        content='{"summary": "prior both-relevant"}',
        priority="low",
        created_at=now,
    )
    genesis_output = MicroOutput(
        tags=["cpu"], salience=0.6, anomaly=False,
        summary="Disk usage creeping.",
        signals_examined=2,
        driving_signals=["cpu_usage"],
    )
    stored = await writer.write(genesis_output, Depth.MICRO, _make_mixed_tick(), db=db)
    assert stored is False, ":both must suppress a Genesis-visible :genesis micro"


async def test_micro_cooldown_both_not_blocked_by_user(db):
    """A recent :user micro (Genesis-invisible) must NOT block a :both micro
    (Genesis-visible) — no cross-partition starvation."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-user-micro2",
        source="reflection",
        type="micro_reflection",
        category="routine:user",
        content='{"summary": "prior user"}',
        priority="low",
        created_at=now,
    )
    both_output = MicroOutput(
        tags=["mixed"], salience=0.6, anomaly=False,
        summary="User activity and disk both moved.",
        signals_examined=2,
        driving_signals=["task_completion_quality", "cpu_usage"],
    )
    stored = await writer.write(both_output, Depth.MICRO, _make_mixed_tick(), db=db)
    assert stored is True, ":user must not block a Genesis-visible :both micro"


async def test_micro_dedup_both_and_genesis_share_partition(db):
    """Anomaly path (cooldown bypassed): structurally-identical :both and
    :genesis micros share the Genesis-visible partition, so the second is a
    duplicate and must be deduped (only one persists)."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    # both anomaly (bypass cooldown), same salience band + same mixed roster
    both_output = MicroOutput(
        tags=["mixed"], salience=0.8, anomaly=True,
        summary="Everything moved.", signals_examined=2,
        driving_signals=["task_completion_quality", "cpu_usage"],  # -> both
    )
    genesis_output = MicroOutput(
        tags=["cpu"], salience=0.8, anomaly=True,
        summary="CPU moved.", signals_examined=2,
        driving_signals=["cpu_usage"],  # -> genesis
    )
    stored_both = await writer.write(both_output, Depth.MICRO, _make_mixed_tick(), db=db)
    stored_genesis = await writer.write(genesis_output, Depth.MICRO, _make_mixed_tick(), db=db)

    assert stored_both is True
    assert stored_genesis is False, ":genesis dups a recent Genesis-visible :both"
    obs = await observations.query(db, source="reflection")
    assert len(obs) == 1


async def test_micro_dedup_user_and_genesis_still_distinct(db):
    """The P2#1 guarantee holds: :user and :genesis are different partitions
    and must both persist (a :user must never drop a :genesis)."""
    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    user_output = MicroOutput(
        tags=["user"], salience=0.8, anomaly=True,
        summary="Task quality moved.", signals_examined=2,
        driving_signals=["task_completion_quality"],  # -> user
    )
    genesis_output = MicroOutput(
        tags=["cpu"], salience=0.8, anomaly=True,
        summary="CPU moved.", signals_examined=2,
        driving_signals=["cpu_usage"],  # -> genesis
    )
    await writer.write(user_output, Depth.MICRO, _make_mixed_tick(), db=db)
    await writer.write(genesis_output, Depth.MICRO, _make_mixed_tick(), db=db)

    obs = await observations.query(db, source="reflection")
    cats = sorted(o["category"] for o in obs)
    assert cats == ["anomaly:genesis", "anomaly:user"]


async def test_micro_cooldown_null_category_is_genesis_visible(db):
    """A recent NULL-category micro is Genesis-visible (genesis_context
    includes ``category IS NULL``), so it must suppress a subsequent genesis
    micro -- the cooldown NOT-LIKE branch must treat NULL as visible."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-null-micro",
        source="reflection",
        type="micro_reflection",
        content='{"summary": "prior, no category"}',
        priority="low",
        created_at=now,
    )
    genesis_output = MicroOutput(
        tags=["cpu"], salience=0.6, anomaly=False,
        summary="Disk usage creeping.",
        signals_examined=2,
        driving_signals=["cpu_usage"],
    )
    stored = await writer.write(genesis_output, Depth.MICRO, _make_mixed_tick(), db=db)
    assert stored is False, "NULL-category prior (Genesis-visible) must suppress a genesis micro"


async def test_micro_cooldown_null_category_does_not_block_user(db):
    """A NULL-category (Genesis-visible) prior must NOT block a :user micro."""
    from datetime import UTC, datetime

    from genesis.db.crud import observations
    from genesis.perception.writer import ResultWriter

    writer = ResultWriter()
    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id="recent-null-micro2",
        source="reflection",
        type="micro_reflection",
        content='{"summary": "prior, no category"}',
        priority="low",
        created_at=now,
    )
    user_output = MicroOutput(
        tags=["user"], salience=0.6, anomaly=False,
        summary="Task quality shifted.",
        signals_examined=2,
        driving_signals=["task_completion_quality"],
    )
    stored = await writer.write(user_output, Depth.MICRO, _make_mixed_tick(), db=db)
    assert stored is True, "Genesis-visible NULL prior must not block a :user micro"


def _tick_with(*names: str) -> TickResult:
    return TickResult(
        tick_id="tick-scope",
        timestamp="2026-03-05T10:00:00+00:00",
        source="scheduled",
        signals=[
            SignalReading(
                name=n, value=0.5, source="test",
                collected_at="2026-03-05T10:00:00+00:00",
            )
            for n in names
        ],
        scores=[],
        classified_depth=Depth.MICRO,
        trigger_reason="test",
    )


def test_relevance_discards_out_of_scope_driving_signal():
    """An LLM citing an out-of-Micro-scope user signal it was never shown must
    not flip relevance to :user — it is discarded and the safe :both fallback
    (full roster) applies, so a genesis micro is not over-excluded."""
    from genesis.perception.writer import ResultWriter

    tick = _tick_with("user_goal_staleness", "cpu_usage")
    rel = ResultWriter._relevance_from_signals(
        tick, ["user_goal_staleness"], excluded_signals={"user_goal_staleness"},
    )
    assert rel == "both"  # NOT "user"


def test_relevance_in_scope_driving_signal_still_classifies():
    """A cited signal that IS in Micro scope still drives classification."""
    from genesis.perception.writer import ResultWriter

    tick = _tick_with("user_goal_staleness", "cpu_usage")
    rel = ResultWriter._relevance_from_signals(
        tick, ["cpu_usage"], excluded_signals={"user_goal_staleness"},
    )
    assert rel == "genesis"


def test_relevance_mixed_scope_keeps_only_in_scope():
    """When a citation mixes in-scope and out-of-scope names, only the
    in-scope one counts."""
    from genesis.perception.writer import ResultWriter

    tick = _tick_with("user_goal_staleness", "cpu_usage")
    rel = ResultWriter._relevance_from_signals(
        tick, ["user_goal_staleness", "cpu_usage"],
        excluded_signals={"user_goal_staleness"},
    )
    assert rel == "genesis"
