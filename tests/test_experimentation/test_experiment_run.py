"""Test the experiment_run MCP tool — Crucible's "run" button.

Hermetic: the LLM A/B (`run_reflection_experiment`) is mocked, so these tests
exercise the tool's wiring (param→variant mapping, persistence pass-through,
verdict shaping, and the unavailable/missing-golden-set guards) without any
live model calls.
"""

from genesis.experimentation.types import ArmResult, ExperimentResult


def _fake_result(name="run_test"):
    return ExperimentResult(
        experiment_name=name,
        control=ArmResult(
            variant_name="ctrl", case_scores=[0.6] * 6, case_results=[True] * 6,
            n_pass=6, mean_score=0.6,
        ),
        treatment=ArmResult(
            variant_name="trt", case_scores=[0.9] * 6, case_results=[True] * 6,
            n_pass=6, mean_score=0.9,
        ),
        winrate={"recommendation": "treatment_wins", "significant": True, "p_value": 0.03},
        n_cases=6,
        errors=0,
        metadata={},
    )


async def test_experiment_run_returns_verdict_and_persists(eval_db, tmp_path, monkeypatch):
    gs = tmp_path / "g.jsonl"
    gs.write_text(
        '{"id":"c1","actual":"x","user_passed":true,'
        '"scorer_config":{"session_context":"ctx","rubric_name":"reflection_quality"}}\n'
    )

    import genesis.mcp.health_mcp as health_mcp_mod

    class _Svc:
        _db = eval_db

    monkeypatch.setattr(health_mcp_mod, "_service", _Svc(), raising=False)

    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr(
        "genesis.experimentation.runner.run_reflection_experiment", fake_run,
    )

    from genesis.mcp.health.experiment_run import _impl_experiment_run

    out = await _impl_experiment_run(
        experiment_name="run_test",
        control_prompt="You are Genesis. Reflect. Output JSON.",
        treatment_prompt="You are Genesis. Reflect carefully. Output JSON.",
        golden_set_path=str(gs),
        limit=6,
    )

    assert out["status"] == "ok"
    assert out["autonomous_action"] is False
    assert out["recommendation"] == "treatment_wins"
    assert out["significant"] is True
    assert out["control"]["mean_score"] == 0.6
    assert out["treatment"]["mean_score"] == 0.9
    assert out["n_cases"] == 6
    assert out["persisted"] is True
    # db threaded through so the run persists; prompts mapped onto variants
    assert captured["db"] is eval_db
    assert captured["control"].system_prompt.startswith("You are Genesis")
    assert captured["treatment"].system_prompt.startswith("You are Genesis")
    # provider defaults thread through (catches a runner signature rename that
    # would otherwise silently fall back to the stale module default judge)
    assert captured["gen_provider"] == "groq-free"
    assert captured["judge_provider"] == "groq-free"


async def test_experiment_run_missing_golden_set(eval_db, monkeypatch):
    import genesis.mcp.health_mcp as health_mcp_mod

    class _Svc:
        _db = eval_db

    monkeypatch.setattr(health_mcp_mod, "_service", _Svc(), raising=False)
    from genesis.mcp.health.experiment_run import _impl_experiment_run

    out = await _impl_experiment_run(
        experiment_name="x",
        control_prompt="a",
        treatment_prompt="b",
        golden_set_path="/nonexistent/path.jsonl",
    )
    assert out["status"] == "error"
    assert "golden set" in out["message"].lower()


async def test_experiment_run_unavailable_without_db(monkeypatch):
    import genesis.mcp.health_mcp as health_mcp_mod

    monkeypatch.setattr(health_mcp_mod, "_service", None, raising=False)
    from genesis.mcp.health.experiment_run import _impl_experiment_run

    out = await _impl_experiment_run(
        experiment_name="x", control_prompt="a", treatment_prompt="b",
    )
    assert out["status"] == "unavailable"
