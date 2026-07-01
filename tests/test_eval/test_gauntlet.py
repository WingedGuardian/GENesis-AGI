"""Tests for the agentic model-roster gauntlet (hermetic — no real CC spawn).

The scorer is exercised through a FakeInvoker that returns a crafted CCOutput
(and can mutate the workdir like a real model would); pytest scoring runs for
real on trivial in-test fixtures. Regression + file-lock logic is unit-tested.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from genesis.cc.exceptions import CCRateLimitError
from genesis.cc.types import CCOutput
from genesis.eval import gauntlet as G
from genesis.eval import gauntlet_regression as GR
from genesis.eval.types import EvalRunSummary, EvalTrigger, ScoredOutput, TaskCategory


def _cc_output(*, via_proxy: bool = False, exit_code: int = 0) -> CCOutput:
    return CCOutput(
        session_id="s1",
        text="done",
        model_used="claude",
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        duration_ms=1234,
        exit_code=exit_code,
        via_proxy=via_proxy,
    )


def _make_fixture(root: Path, *, green: bool) -> G.GauntletFixture:
    """A minimal fixture project. green=True → tests pass as-is; else they fail."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "conftest.py").write_text("# sys.path marker\n")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def f():\n    return 1\n")
    tests = root / "tests"
    tests.mkdir()
    expected = 1 if green else 2
    (tests / "test_x.py").write_text(
        f"from pkg.core import f\n\ndef test_f():\n    assert f() == {expected}\n"
    )
    return G.GauntletFixture(
        name="mini", path=root, task="fix it", timeout_s=60, difficulty="test",
    )


class _FakeInvoker:
    def __init__(self, behavior):
        self._behavior = behavior

    async def run(self, inv):
        return await self._behavior(inv)


@pytest.fixture
def tmp_root(tmp_path):
    d = tmp_path / "gtmp"
    d.mkdir()
    return d


async def test_pass_when_green_and_untouched(tmp_path, tmp_root):
    fx = _make_fixture(tmp_path / "fx", green=True)

    async def noop(inv):
        return _cc_output(via_proxy=False)

    res = await G._run_one_fixture(_FakeInvoker(noop), {}, None, fx, tmp_root)
    assert res.passed is True
    assert res.skipped is False
    assert res.score == 1.0


async def test_fail_when_tests_stay_red(tmp_path, tmp_root):
    fx = _make_fixture(tmp_path / "fx", green=False)

    async def noop(inv):
        return _cc_output(via_proxy=False)

    res = await G._run_one_fixture(_FakeInvoker(noop), {}, None, fx, tmp_root)
    assert res.passed is False
    assert res.skipped is False
    assert "pytest_exit" in res.scorer_detail


async def test_cheat_fail_when_protected_surface_mutated(tmp_path, tmp_root):
    fx = _make_fixture(tmp_path / "fx", green=False)

    async def cheat(inv):
        # Rewrite the test to trivially pass instead of fixing the source.
        test_file = Path(inv.working_dir) / "tests" / "test_x.py"
        test_file.write_text("def test_f():\n    assert True\n")
        return _cc_output(via_proxy=False)

    res = await G._run_one_fixture(_FakeInvoker(cheat), {}, None, fx, tmp_root)
    assert res.passed is False
    assert res.skipped is False
    assert "protected" in res.scorer_detail.lower()


async def test_skip_on_infra_error(tmp_path, tmp_root):
    fx = _make_fixture(tmp_path / "fx", green=True)

    async def boom(inv):
        raise CCRateLimitError("429 balance exhausted")

    res = await G._run_one_fixture(_FakeInvoker(boom), {}, None, fx, tmp_root)
    assert res.skipped is True
    assert res.passed is False
    assert "infra" in res.scorer_detail.lower()


async def test_skip_on_routing_mismatch(tmp_path, tmp_root):
    fx = _make_fixture(tmp_path / "fx", green=True)

    async def wrong_route(inv):
        # Native run (overrides={}) expects via_proxy=False; return True → mismatch.
        return _cc_output(via_proxy=True)

    res = await G._run_one_fixture(_FakeInvoker(wrong_route), {}, None, fx, tmp_root)
    assert res.skipped is True
    assert "routing mismatch" in res.scorer_detail.lower()


def test_protected_hash_detects_test_edit_and_rogue_conftest(tmp_path):
    root = tmp_path / "fx"
    root.mkdir()
    _make_fixture(root, green=False)
    h0 = G._protected_hash(root)
    # Editing the SOURCE package must not change the protected hash.
    (root / "pkg" / "core.py").write_text("def f():\n    return 2  # fixed\n")
    assert G._protected_hash(root) == h0
    # A rogue nested conftest must change it.
    (root / "pkg" / "conftest.py").write_text("# rogue\n")
    assert G._protected_hash(root) != h0


def test_file_lock_is_mutually_exclusive():
    fh = G._acquire_lock("test-model-xyz")
    try:
        with pytest.raises(G.GauntletBusyError):
            G._acquire_lock("test-model-xyz")
    finally:
        G._release_lock(fh)
    # Released → can acquire again.
    fh2 = G._acquire_lock("test-model-xyz")
    G._release_lock(fh2)


def test_load_committed_fixtures():
    fx = {f.name for f in G.load_gauntlet_fixtures()}
    assert {"statslib_bugs", "intervals_multifile", "calc_longhorizon"} <= fx


# ---- regression detection ----

def _summary(model, run_id, *, passed, failed, skipped=0, results=None):
    return EvalRunSummary(
        run_id=run_id, model_id=model, model_profile=model, dataset="gauntlet",
        trigger=EvalTrigger.SCHEDULE, task_category=TaskCategory.AGENTIC,
        total_cases=passed + failed + skipped, passed_cases=passed,
        failed_cases=failed, skipped_cases=skipped, aggregate_score=0.0,
        results=results or [],
    )


class _FakePipeline:
    def __init__(self):
        self.alerts = []

    async def submit_raw(self, text, request, **kw):
        self.alerts.append((text, request))


async def test_regression_fires_on_pass_then_fail(monkeypatch):
    model = "glm-5.2"
    # prior run PASSED (failed=0, passed>0); current FAILED.
    async def fake_get_runs(db, *, model_id, dataset, limit):
        return [
            {"id": "cur", "passed_cases": 1, "failed_cases": 2},
            {"id": "old", "passed_cases": 3, "failed_cases": 0},
        ]
    created = {}
    async def fake_get_proposal(db, pid):
        return None
    async def fake_create_proposal(db, **kw):
        created.update(kw)
    monkeypatch.setattr(GR, "get_runs", fake_get_runs)
    import genesis.db.crud.ego as ego_crud
    monkeypatch.setattr(ego_crud, "get_proposal", fake_get_proposal)
    monkeypatch.setattr(ego_crud, "create_proposal", fake_create_proposal)

    pipe = _FakePipeline()
    results = [ScoredOutput(case_id="calc", passed=False, score=0.0, actual_output="",
                            scorer_type="agentic_pytest")]
    reg = await GR.check_gauntlet_regression(
        db=None, summary=_summary(model, "cur", passed=1, failed=2, results=results),
        outreach_pipeline=pipe,
    )
    assert reg is not None
    assert reg["model_id"] == model
    assert len(pipe.alerts) == 1
    assert created.get("action_type") == "gauntlet_regression"


async def test_no_regression_on_cold_start_fail(monkeypatch):
    # FAIL but NO prior pass → not a regression.
    async def fake_get_runs(db, *, model_id, dataset, limit):
        return [{"id": "cur", "passed_cases": 0, "failed_cases": 3}]
    monkeypatch.setattr(GR, "get_runs", fake_get_runs)
    pipe = _FakePipeline()
    reg = await GR.check_gauntlet_regression(
        db=None, summary=_summary("m", "cur", passed=0, failed=3), outreach_pipeline=pipe,
    )
    assert reg is None
    assert pipe.alerts == []


async def test_no_regression_when_all_skipped(monkeypatch):
    # All fixtures skipped (e.g. peer out of balance) → inconclusive, no alert.
    called = {"get_runs": False}
    async def fake_get_runs(db, *, model_id, dataset, limit):
        called["get_runs"] = True
        return []
    monkeypatch.setattr(GR, "get_runs", fake_get_runs)
    pipe = _FakePipeline()
    reg = await GR.check_gauntlet_regression(
        db=None, summary=_summary("m", "cur", passed=0, failed=0, skipped=3),
        outreach_pipeline=pipe,
    )
    assert reg is None
    assert pipe.alerts == []
    assert called["get_runs"] is False  # short-circuits before the DB query


async def test_regression_idempotent_when_proposal_exists(monkeypatch):
    async def fake_get_runs(db, *, model_id, dataset, limit):
        return [{"id": "old", "passed_cases": 3, "failed_cases": 0}]
    async def fake_get_proposal(db, pid):
        return {"id": pid}  # already exists
    create_calls = []
    async def fake_create_proposal(db, **kw):
        create_calls.append(kw)
    monkeypatch.setattr(GR, "get_runs", fake_get_runs)
    import genesis.db.crud.ego as ego_crud
    monkeypatch.setattr(ego_crud, "get_proposal", fake_get_proposal)
    monkeypatch.setattr(ego_crud, "create_proposal", fake_create_proposal)
    pipe = _FakePipeline()
    reg = await GR.check_gauntlet_regression(
        db=None, summary=_summary("m", "cur", passed=1, failed=1), outreach_pipeline=pipe,
    )
    assert reg is None
    assert create_calls == []
    assert pipe.alerts == []
