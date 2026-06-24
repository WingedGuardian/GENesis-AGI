"""Hermetic tests for the evo_run MCP tool (recommend-only wiring).

The live router build + run_evo are stubbed; variant building is deterministic.
These exercise the tool's wiring, verdict mapping, and summary persistence with
no model calls.
"""

import json

from genesis.experimentation.evo import EvoResult
from genesis.experimentation.types import CognitiveVariant


class _DummyRouter:
    async def close(self):
        pass


def _golden(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({
        "id": "c0", "actual": "x", "user_passed": True,
        "scorer_config": {"session_context": "ctx", "rubric_name": "reflection_quality"},
    }) + "\n")
    return p


def _stub_routers(monkeypatch):
    monkeypatch.setattr(
        "genesis.mcp.health.evo_run._build_routers",
        lambda g, j: (_DummyRouter(), _DummyRouter(), object()),
    )


def _stub_variants(monkeypatch, n=3):
    monkeypatch.setattr(
        "genesis.experimentation.evo.build_directive_variants",
        lambda base, count: [
            CognitiveVariant(name=f"evo_v{i}", system_prompt=f"{base} :: d{i}")
            for i in range(min(count, n))
        ],
    )


async def test_evo_run_recommends_winner(tmp_path, monkeypatch):
    _stub_routers(monkeypatch)
    _stub_variants(monkeypatch)

    winner = CognitiveVariant(name="evo_v1", description="more concise", system_prompt="WINNER PROMPT TEXT")

    async def fake_run_evo(*, base, candidates, golden_set_path, config, gen_router, judge):
        return EvoResult(
            winner=winner,
            winner_winrate={"recommendation": "treatment_wins", "p_value": 0.001},
            holdout_winrate={"recommendation": "treatment_wins", "p_value": 0.01},
            candidates_evaluated=len(candidates), survivors=2, note="confirmed",
            holdout_disjoint=True,
        )

    monkeypatch.setattr("genesis.experimentation.evo.run_evo", fake_run_evo)

    from genesis.mcp.health.evo_run import _impl_evo_run

    out = await _impl_evo_run(
        base_prompt="BASE", n_variants=3, golden_set_path=str(_golden(tmp_path)),
        eval_limit=2, holdout_limit=2,
    )
    assert out["status"] == "ok"
    assert out["autonomous_action"] is False
    assert out["winner"]["name"] == "evo_v1"
    assert out["winner"]["full_prompt"] == "WINNER PROMPT TEXT"
    assert out["winner"]["approach"] == "more concise"
    assert out["survivors"] == 2
    assert out["holdout_disjoint"] is True
    assert out["persisted_run_id"] is None  # no _service db in this test


async def test_evo_run_persists_summary(eval_db, tmp_path, monkeypatch):
    _stub_routers(monkeypatch)
    _stub_variants(monkeypatch)

    import genesis.mcp.health_mcp as health_mcp_mod

    class _Svc:
        _db = eval_db

    monkeypatch.setattr(health_mcp_mod, "_service", _Svc(), raising=False)

    async def fake_run_evo(*, base, candidates, golden_set_path, config, gen_router, judge):
        return EvoResult(
            winner=CognitiveVariant(name="evo_v0", system_prompt="W"),
            winner_winrate={"recommendation": "treatment_wins", "treatment_mean_score": 0.8},
            holdout_winrate={"recommendation": "treatment_wins"},
            candidates_evaluated=3, survivors=1, note="ok", holdout_disjoint=True,
        )

    monkeypatch.setattr("genesis.experimentation.evo.run_evo", fake_run_evo)

    from genesis.mcp.health.evo_run import _impl_evo_run

    out = await _impl_evo_run(base_prompt="BASE", golden_set_path=str(_golden(tmp_path)))
    assert out["status"] == "ok"
    assert out["persisted_run_id"] is not None

    # one evo_summary row written, surfaced by experiment_status under evo_runs
    from genesis.mcp.health.experiment_status import _impl_experiment_status

    status = await _impl_experiment_status()
    assert len(status["evo_runs"]) == 1
    assert status["evo_runs"][0]["winner"] == "evo_v0"
    assert status["evo_runs"][0]["survivors"] == 1


async def test_evo_run_no_winner(tmp_path, monkeypatch):
    _stub_routers(monkeypatch)
    _stub_variants(monkeypatch, n=1)

    async def fake_run_evo(**kw):
        return EvoResult(
            winner=None, winner_winrate=None, holdout_winrate=None,
            candidates_evaluated=1, survivors=0, note="no variant beat control",
        )

    monkeypatch.setattr("genesis.experimentation.evo.run_evo", fake_run_evo)

    from genesis.mcp.health.evo_run import _impl_evo_run

    out = await _impl_evo_run(base_prompt="BASE", golden_set_path=str(_golden(tmp_path)))
    assert out["status"] == "ok"
    assert out["winner"] is None
    assert out["survivors"] == 0


async def test_evo_run_missing_golden_set():
    from genesis.mcp.health.evo_run import _impl_evo_run

    out = await _impl_evo_run(base_prompt="BASE", golden_set_path="/nonexistent.jsonl")
    assert out["status"] == "error"
    assert "golden set" in out["message"].lower()


async def test_evo_run_no_variants_built(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "genesis.experimentation.evo.build_directive_variants", lambda base, count: [],
    )

    from genesis.mcp.health.evo_run import _impl_evo_run

    out = await _impl_evo_run(base_prompt="BASE", golden_set_path=str(_golden(tmp_path)))
    assert out["status"] == "error"
    assert "no variants" in out["message"].lower()


def test_make_router_cc_provider_returns_cc_cli_router():
    from genesis.experimentation.cc_router import CCCliRouter
    from genesis.mcp.health.evo_run import _make_router

    r = _make_router("cc-haiku", None, None)
    assert isinstance(r, CCCliRouter)
    assert r._model == "haiku"
