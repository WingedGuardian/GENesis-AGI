"""hook_fold orchestration tests — the contract the hook depends on."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from genesis.session_awareness import hook_fold
from genesis.session_awareness.statefiles import load_state

DIM = 8
T0 = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def blend(a: list[float], b: list[float], t: float) -> list[float]:
    return [(1 - t) * x + t * y for x, y in zip(a, b, strict=True)]


def _turns(n: int):
    """n same-theme turns: near-identical vectors, one minute apart."""
    for i in range(n):
        yield blend(unit(0), unit(1), 0.02 * i), T0 + timedelta(minutes=i)


def test_settled_theme_fires_and_persists(tmp_path):
    results = []
    for vec, now in _turns(4):
        results.append(
            hook_fold(
                session_id="sess-f",
                vector=vec,
                prompt_keywords=["genesis", "memory"],
                base_dir=tmp_path,
                now=now,
            )
        )
    assert all(r is not None for r in results)
    fired = [r for r in results if r["fired"]]
    assert len(fired) == 1
    assert fired[0]["reason"] == "fire"
    # Statefile is the durable record
    state = load_state("sess-f", base=tmp_path, now=T0 + timedelta(minutes=5))
    assert state["fired_count"] == 1
    assert state["fired"][0]["turn"] >= 3
    assert state["worker_pending_since"] is not None


def test_no_double_fire_same_region(tmp_path):
    fires = 0
    for vec, now in _turns(10):
        r = hook_fold(
            session_id="sess-d",
            vector=vec,
            prompt_keywords=["kw"],
            base_dir=tmp_path,
            now=now,
        )
        fires += int(r["fired"])
    assert fires == 1  # same region: near_fired_region gates re-fires


def test_returns_none_on_bad_input(tmp_path):
    assert hook_fold(
        session_id="", vector=unit(0), prompt_keywords=[], base_dir=tmp_path,
    ) is None
    assert hook_fold(
        session_id="s", vector=[], prompt_keywords=[], base_dir=tmp_path,
    ) is None
    # Garbage vector type must not raise (fail-open contract)
    assert hook_fold(
        session_id="s",
        vector=["not", "floats"],  # type: ignore[list-item]
        prompt_keywords=[],
        base_dir=tmp_path,
        now=T0,
    ) is None


def test_statefile_is_json_readable(tmp_path):
    for vec, now in _turns(2):
        hook_fold(
            session_id="sess-j",
            vector=vec,
            prompt_keywords=["alpha"],
            base_dir=tmp_path,
            now=now,
        )
    raw = json.loads((tmp_path / "sess-j" / "session_theme.json").read_text())
    assert raw["ema_turns"] == 2
    assert raw["session_id"] == "sess-j"
