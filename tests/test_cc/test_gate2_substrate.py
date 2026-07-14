"""WS-3 gate-2 substrate: reflection deltas carry the run-level origin aggregate.

The chain under test: an external-origin session active in the material window
-> ``reflection_window_origin`` says external -> ``_process_light_output``
stamps the ``user_model_delta`` observation -> the USER_KNOWLEDGE emit can
aggregate real provenance (unit-covered in test_user_model / test_db).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.awareness.types import Depth, TickResult
from genesis.cc.reflection_bridge._output import store_reflection_output
from genesis.cc.types import CCOutput
from genesis.db.crud import cc_sessions

pytestmark = pytest.mark.asyncio


def _tick(now: str) -> TickResult:
    return TickResult(
        tick_id="tick-g2", timestamp=now, source="scheduled",
        signals=[], scores=[], classified_depth=Depth.LIGHT,
        trigger_reason="test",
    )


def _output(field: str) -> CCOutput:
    return CCOutput(
        session_id="refl-g2",
        text=json.dumps({
            "assessment": "ok", "patterns": [], "recommendations": [],
            "confidence": 0.9, "focus_area": "user_impact",
            "escalate_to_deep": False, "escalation_reason": "",
            "user_model_updates": [{
                "field": field, "value": "test-value",
                "evidence": "synthetic", "confidence": 0.95,
            }],
            "surplus_candidates": [],
        }),
        model_used="haiku", cost_usd=0.0, input_tokens=1, output_tokens=1,
        duration_ms=1, exit_code=0,
    )


async def _delta_origin(db, field: str) -> str | None:
    rows = await db.execute_fetchall(
        "SELECT origin_class FROM observations "
        "WHERE type='user_model_delta' AND content LIKE ?",
        (f'%"{field}"%',),
    )
    assert rows, f"delta for {field} not written"
    return rows[0][0]


async def test_delta_stamped_external_when_external_session_in_window(db):
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        db, id="s-ext", session_type="background_task", model="sonnet",
        started_at=now, last_activity_at=now, source_tag="inbox_evaluation",
        origin_class="external_untrusted",
    )
    await store_reflection_output(Depth.LIGHT, _tick(now), _output("field_ext"), db=db)
    assert await _delta_origin(db, "field_ext") == "external_untrusted"


async def test_delta_stamped_first_party_when_window_quiet(db):
    now = datetime.now(UTC).isoformat()
    await cc_sessions.create(
        db, id="s-fp", session_type="background_reflection", model="sonnet",
        started_at=now, last_activity_at=now, source_tag="reflection_light",
    )
    await store_reflection_output(Depth.LIGHT, _tick(now), _output("field_fp"), db=db)
    assert await _delta_origin(db, "field_fp") == "first_party"


async def test_stale_external_session_does_not_taint(db):
    now = datetime.now(UTC)
    stale = (now - timedelta(hours=3)).isoformat()
    await cc_sessions.create(
        db, id="s-old", session_type="background_task", model="sonnet",
        started_at=stale, last_activity_at=stale, source_tag="inbox_evaluation",
        status="completed", origin_class="external_untrusted",
    )
    await store_reflection_output(
        Depth.LIGHT, _tick(now.isoformat()), _output("field_stale"), db=db,
    )
    assert await _delta_origin(db, "field_stale") == "first_party"
