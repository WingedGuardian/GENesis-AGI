"""Test the read-only experiment_status MCP surface."""

from genesis.experimentation.persistence import persist_experiment
from genesis.experimentation.types import ArmResult, ExperimentResult


def _result(name="mcp_test"):
    return ExperimentResult(
        experiment_name=name,
        control=ArmResult(
            variant_name="ctrl", case_scores=[0.8] * 6, case_results=[True] * 6,
            n_pass=6, mean_score=0.8,
        ),
        treatment=ArmResult(
            variant_name="trt", case_scores=[0.1] * 6, case_results=[False] * 6,
            n_pass=0, mean_score=0.1,
        ),
        winrate={"recommendation": "control_wins", "significant": True, "n_control_wins": 6},
        n_cases=6,
        errors=0,
        metadata={"rubric_name": "reflection_quality"},
    )


async def test_experiment_status_surfaces_recommendation(eval_db, monkeypatch):
    await persist_experiment(eval_db, _result(), gen_provider="g", judge_provider="nvidia-nim-deepseek")

    import genesis.mcp.health_mcp as health_mcp_mod

    class _Svc:
        _db = eval_db

    monkeypatch.setattr(health_mcp_mod, "_service", _Svc(), raising=False)

    from genesis.mcp.health.experiment_status import _impl_experiment_status

    out = await _impl_experiment_status()
    assert out["status"] == "ok"
    assert out["autonomous_action"] is False
    assert len(out["experiments"]) == 1

    exp = out["experiments"][0]
    assert exp["recommendation"] == "control_wins"
    assert exp["experiment"] == "experiment:reflection:mcp_test"
    assert exp["control"]["variant"] == "ctrl"
    assert exp["treatment"]["variant"] == "trt"
    assert exp["winrate"]["n_control_wins"] == 6
    assert exp["judge_provider"] == "nvidia-nim-deepseek"


async def test_experiment_status_unavailable_without_db(monkeypatch):
    import genesis.mcp.health_mcp as health_mcp_mod

    monkeypatch.setattr(health_mcp_mod, "_service", None, raising=False)
    from genesis.mcp.health.experiment_status import _impl_experiment_status

    out = await _impl_experiment_status()
    assert out["status"] == "unavailable"
