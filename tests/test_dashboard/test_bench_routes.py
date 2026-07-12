"""Tests for the bench A/B dashboard route (/api/genesis/eval/bench).

The route is glue (guard → query → shape → jsonify); the shaping is unit-tested
in test_eval/test_bench_surface.py and the query in test_eval/test_db_bench.py.
Here we pin the wiring: registration, the bootstrap guard, and that
judge_calibrated / the headline survive to the JSON body.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _genesis_row() -> dict:
    meta = {
        "judge_calibrated": False,
        "rubric": "bench_task_success",
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
                "significant": False,
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
        "model_profile": "bench:genesis",
        "aggregate_score": 0.8111,
        "created_at": "2026-07-10T04:47:52+00:00",
        "metadata_json": json.dumps(meta),
    }


def _rt(*, bootstrapped=True, db=True):
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt._db = MagicMock() if db else None
    return rt


def test_route_returns_shaped_surface(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.eval.db.get_bench_comparisons",
            new=AsyncMock(return_value=[_genesis_row()]),
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/eval/bench")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    assert data["judge_calibrated"] is False
    assert data["latest"]["genesis_mean"] == 0.8111
    assert data["latest"]["bare_mean"] == 0.6444
    assert data["latest"]["recommendation"] == "insufficient_data"


def test_route_empty_data_is_200(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.eval.db.get_bench_comparisons",
            new=AsyncMock(return_value=[]),
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/eval/bench")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 0
    assert data["latest"] is None


def test_route_503_when_not_bootstrapped(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt(bootstrapped=False)
        resp = client.get("/api/genesis/eval/bench")
    assert resp.status_code == 503
