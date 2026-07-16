"""M7 fix: the per-delta confidence gate admits >=0.85 on the CC-bridge path.

Regression lock for the user_model_delta starvation: the original 0.90 gate
sat above the light model's median output confidence (avg 0.86), so almost
no delta ever passed — 2 in 3.5 months. Boundary: 0.85 stored, 0.84 dropped.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from genesis.awareness.types import Depth, TickResult
from genesis.cc.reflection_bridge._output import store_reflection_output
from genesis.cc.types import CCOutput
from genesis.perception.types import MIN_DELTA_CONFIDENCE

pytestmark = pytest.mark.asyncio


def _tick(now: str) -> TickResult:
    return TickResult(
        tick_id="tick-m7", timestamp=now, source="scheduled",
        signals=[], scores=[], classified_depth=Depth.LIGHT,
        trigger_reason="test",
    )


def _output(deltas: list[dict]) -> CCOutput:
    return CCOutput(
        session_id="refl-m7",
        text=json.dumps({
            "assessment": "ok", "patterns": [], "recommendations": [],
            "confidence": 0.9, "focus_area": "user_impact",
            "escalate_to_deep": False, "escalation_reason": "",
            "user_model_updates": deltas,
            "surplus_candidates": [],
        }),
        model_used="haiku", cost_usd=0.0, input_tokens=1, output_tokens=1,
        duration_ms=1, exit_code=0,
    )


async def test_gate_boundary_admits_085_drops_below(db):
    assert MIN_DELTA_CONFIDENCE == 0.85
    now = datetime.now(UTC).isoformat()
    deltas = [
        {"field": "at_boundary", "value": "v", "evidence": "e", "confidence": 0.85},
        {"field": "below_boundary", "value": "v", "evidence": "e", "confidence": 0.84},
        {"field": "filler_tier", "value": "v", "evidence": "e", "confidence": 0.80},
    ]
    await store_reflection_output(Depth.LIGHT, _tick(now), _output(deltas), db=db)

    rows = await db.execute_fetchall(
        "SELECT content FROM observations WHERE type='user_model_delta'",
    )
    stored = {json.loads(r[0])["field"] for r in rows}
    assert stored == {"at_boundary"}
