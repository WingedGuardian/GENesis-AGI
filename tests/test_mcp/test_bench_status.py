"""Tests for the bench_status health MCP tool (WS-1 A5 read surface).

Lets Genesis read its own Genesis-vs-bare A/B win-rate in-session. Shaping is
shared with the dashboard route (eval/bench/surface.py); here we pin the MCP
wiring: the service/DB guard, the read-error → 'unavailable' path (never
silently-wrong), and limit forwarding.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import genesis.mcp.health as health_pkg
from genesis.mcp.health.bench_status import _impl_bench_status


def _genesis_row() -> dict:
    meta = {
        "judge_calibrated": False,
        "rubric_version": "1.0.0",
        "task_set_version": "pilot-v1",
        "invalid": False,
        "stats": {
            "score_winrate": {
                "n_cases": 9,
                "control_mean_score": 0.6444,
                "treatment_mean_score": 0.8111,
                "mean_delta": 0.1667,
                "n_control_wins": 0,
                "n_treatment_wins": 2,
                "n_ties": 7,
                "p_value": 0.5,
                "recommendation": "insufficient_data",
            },
            "pass_winrate": {
                "control_pass_rate": 0.6667,
                "treatment_pass_rate": 0.7778,
            },
        },
    }
    return {
        "id": "b2be8b5fad67-genesis",
        "created_at": "2026-07-10T04:47:52+00:00",
        "aggregate_score": 0.8111,
        "metadata_json": json.dumps(meta),
    }


def _service_with_db():
    svc = MagicMock()
    svc._db = MagicMock()
    return svc


async def test_impl_returns_surface(monkeypatch):
    monkeypatch.setattr(health_pkg, "_service", _service_with_db())

    async def fake_q(db, *, limit=12):
        return [_genesis_row()]

    monkeypatch.setattr("genesis.eval.db.get_bench_comparisons", fake_q)
    out = await _impl_bench_status()
    assert out["status"] == "ok"
    assert out["count"] == 1
    assert out["judge_calibrated"] is False
    assert out["latest"]["genesis_mean"] == 0.8111
    assert out["latest"]["recommendation"] == "insufficient_data"


async def test_impl_unavailable_when_no_service(monkeypatch):
    monkeypatch.setattr(health_pkg, "_service", None)
    out = await _impl_bench_status()
    assert out["status"] == "unavailable"


async def test_impl_unavailable_on_read_error(monkeypatch):
    monkeypatch.setattr(health_pkg, "_service", _service_with_db())

    async def boom(db, *, limit=12):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("genesis.eval.db.get_bench_comparisons", boom)
    out = await _impl_bench_status()
    assert out["status"] == "unavailable"


async def test_impl_forwards_limit(monkeypatch):
    monkeypatch.setattr(health_pkg, "_service", _service_with_db())
    captured = {}

    async def fake_q(db, *, limit=12):
        captured["limit"] = limit
        return []

    monkeypatch.setattr("genesis.eval.db.get_bench_comparisons", fake_q)
    out = await _impl_bench_status(limit=3)
    assert captured["limit"] == 3
    assert out["status"] == "ok"
    assert out["count"] == 0
