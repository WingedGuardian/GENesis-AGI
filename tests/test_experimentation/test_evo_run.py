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


# ---------------------------------------------------------------------------
# Auto-file path (PR-B): promote_evo_winner + canonical base + propose gate
# ---------------------------------------------------------------------------

import aiosqlite  # noqa: E402
import pytest  # noqa: E402

from genesis.db.crud import ego as ego_crud  # noqa: E402
from genesis.db.schema import create_all_tables  # noqa: E402


@pytest.fixture
async def cvdb():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _winner_result(*, holdout_p=0.01, t_mean=0.82, c_mean=0.70):
    return EvoResult(
        winner=CognitiveVariant(
            name="evo_v0", description="be more concise",
            system_prompt="CANONICAL\n\nbe more concise",
        ),
        winner_winrate={"recommendation": "treatment_wins", "p_value": 0.002,
                        "treatment_mean_score": t_mean},
        holdout_winrate={"recommendation": "treatment_wins", "p_value": holdout_p,
                         "treatment_mean_score": t_mean, "control_mean_score": c_mean},
        candidates_evaluated=6, survivors=2, note="confirmed", holdout_disjoint=True,
    )


async def test_promote_files_winner_proposal(cvdb):
    from genesis.mcp.health.evo_run import promote_evo_winner

    pid = await promote_evo_winner(
        cvdb, _winner_result(), gen_provider="groq-free", judge_provider="groq-free",
    )
    assert pid is not None
    prop = await ego_crud.get_proposal(cvdb, pid)
    assert prop["action_type"] == "cognitive_variant_promotion"
    assert prop["status"] == "pending"          # dashboard-visible, awaiting approval
    assert float(prop["confidence"]) >= 0.75    # 1 - holdout_p = 0.99
    outputs = json.loads(prop["expected_outputs"])
    assert outputs["full_prompt"] == "CANONICAL\n\nbe more concise"
    assert outputs["approach"] == "be more concise"


async def test_promote_self_scoring_caveat(cvdb):
    """Same gen+judge provider → the proposal rationale carries a self-scoring
    caveat; cross-provider → it does not."""
    from genesis.mcp.health.evo_run import promote_evo_winner

    same = await promote_evo_winner(
        cvdb, _winner_result(), gen_provider="groq-free", judge_provider="groq-free",
    )
    rat_same = (await ego_crud.get_proposal(cvdb, same))["rationale"]
    assert "CAVEAT" in rat_same and "share a provider" in rat_same

    # distinct winner so content-hash dedup doesn't skip the second proposal
    cross_result = _winner_result()
    cross_result = EvoResult(
        winner=CognitiveVariant(
            name="evo_v1", description="be deeper",
            system_prompt="CANONICAL\n\nbe deeper",
        ),
        winner_winrate=cross_result.winner_winrate,
        holdout_winrate=cross_result.holdout_winrate,
        candidates_evaluated=6, survivors=2, note="x", holdout_disjoint=True,
    )
    cross = await promote_evo_winner(
        cvdb, cross_result, gen_provider="groq-free", judge_provider="cc-haiku",
    )
    rat_cross = (await ego_crud.get_proposal(cvdb, cross))["rationale"]
    assert "CAVEAT" not in rat_cross


async def test_promote_no_winner_files_nothing(cvdb):
    from genesis.mcp.health.evo_run import promote_evo_winner

    no_winner = EvoResult(
        winner=None, winner_winrate=None, holdout_winrate=None,
        candidates_evaluated=6, survivors=0, note="none",
    )
    pid = await promote_evo_winner(
        cvdb, no_winner, gen_provider="g", judge_provider="j",
    )
    assert pid is None


async def test_promote_below_floor_files_nothing(cvdb):
    """A winner whose held-out p maps to confidence < 0.75 is NOT filed
    (belt-and-suspenders to the held-out gate)."""
    from genesis.mcp.health.evo_run import promote_evo_winner

    pid = await promote_evo_winner(
        cvdb, _winner_result(holdout_p=0.30),  # confidence 0.70
        gen_provider="g", judge_provider="j",
    )
    assert pid is None


async def test_promote_missing_holdout_p_files_nothing(cvdb):
    from genesis.mcp.health.evo_run import promote_evo_winner

    res = _winner_result()
    res = EvoResult(
        winner=res.winner, winner_winrate=res.winner_winrate,
        holdout_winrate={"recommendation": "treatment_wins"},  # no p_value
        candidates_evaluated=6, survivors=2, note="x", holdout_disjoint=True,
    )
    pid = await promote_evo_winner(cvdb, res, gen_provider="g", judge_provider="j")
    assert pid is None


def test_resolve_canonical_ignores_overlay(tmp_path, monkeypatch):
    """The canonical base must read the REPO prompt, NOT the overlay — else
    repeated promotions stack directives unboundedly."""
    from genesis.mcp.health.evo_run import _resolve_canonical_base_prompt

    overlay = tmp_path / "REFLECTION_DEEP.md"
    overlay.write_text("OVERLAY_SENTINEL should never be the measurement base")
    monkeypatch.setenv("GENESIS_REFLECTION_PROMPT_DIR", str(tmp_path))

    base = _resolve_canonical_base_prompt()
    assert "OVERLAY_SENTINEL" not in base
    assert base.strip()  # the real repo prompt, non-empty


async def test_evo_run_propose_false_skips_filing(cvdb, tmp_path, monkeypatch):
    _stub_routers(monkeypatch)
    _stub_variants(monkeypatch)
    import genesis.mcp.health_mcp as health_mcp_mod

    class _Svc:
        _db = cvdb

    monkeypatch.setattr(health_mcp_mod, "_service", _Svc(), raising=False)

    async def fake_run_evo(**kw):
        return _winner_result()

    monkeypatch.setattr("genesis.experimentation.evo.run_evo", fake_run_evo)

    from genesis.mcp.health.evo_run import _impl_evo_run

    out = await _impl_evo_run(
        base_prompt="BASE", golden_set_path=str(_golden(tmp_path)), propose=False,
    )
    assert out["status"] == "ok"
    assert out["proposal_id"] is None
    assert await ego_crud.list_proposals(cvdb, status="pending") == []


async def test_evo_run_propose_true_files_winner(cvdb, tmp_path, monkeypatch):
    _stub_routers(monkeypatch)
    _stub_variants(monkeypatch)
    import genesis.mcp.health_mcp as health_mcp_mod

    class _Svc:
        _db = cvdb

    monkeypatch.setattr(health_mcp_mod, "_service", _Svc(), raising=False)

    async def fake_run_evo(**kw):
        return _winner_result()

    monkeypatch.setattr("genesis.experimentation.evo.run_evo", fake_run_evo)

    from genesis.mcp.health.evo_run import _impl_evo_run

    out = await _impl_evo_run(
        base_prompt="BASE", golden_set_path=str(_golden(tmp_path)), propose=True,
    )
    assert out["status"] == "ok"
    assert out["proposal_id"] is not None
    prop = await ego_crud.get_proposal(cvdb, out["proposal_id"])
    assert prop["action_type"] == "cognitive_variant_promotion"
