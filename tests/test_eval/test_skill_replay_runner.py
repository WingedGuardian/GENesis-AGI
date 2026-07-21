"""Runner tests for the skill-replay gate — fake invoker + fake scorer, no CC.

The runner reuses the bench's run_arm/judge_arm skip-semantics and its own
verdict. These tests drive the whole paired loop deterministically and assert
the verdict, that an infra skip shrinks N, and that nothing mutates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import genesis.eval.skill_replay.runner as runner_mod
from genesis.eval.skill_replay.runner import run_skill_replay
from genesis.eval.skill_replay.types import (
    VERDICT_INCONCLUSIVE,
    VERDICT_NET_POSITIVE,
    VERDICT_REGRESSION,
    SkillReplayConfig,
)

_FIXTURE = Path(__file__).parent / "skill_golden_fixtures" / "voice_master_fixture.jsonl"
_CFG = SkillReplayConfig(epsilon=0.05, min_pairs=3)


@dataclass
class _FakeOutput:
    text: str
    is_error: bool = False
    error_message: str = ""
    model_used: str = "fake-model"
    duration_ms: float = 10.0
    cost_usd: float = 0.0
    input_tokens: int = 1
    output_tokens: int = 1


class _FakeInvoker:
    """Returns 'OLD'/'NEW' by which content is pinned; can error the OLD arm of
    named tasks to exercise the infra-skip path."""

    def __init__(self, error_old_for: frozenset[str] = frozenset()):
        self._error_old_for = error_old_for

    async def run(self, inv):
        is_new = "NEWBODY" in (inv.system_prompt or "")
        if not is_new and any(s in inv.prompt for s in self._error_old_for):
            return _FakeOutput(text="", is_error=True, error_message="infra boom")
        return _FakeOutput(text="NEW" if is_new else "OLD")


class _FakeScorer:
    """Scores by (task-prompt substring, arm). Default 0.5 (a failing tie)."""

    def __init__(self, scores: dict[tuple[str, str], float]):
        self._scores = scores

    async def score_async(self, *, actual, expected, config):
        arm = "new" if actual == "NEW" else "old"
        task_prompt = config.get("task_prompt", "")
        score = 0.5
        for (substr, a), val in self._scores.items():
            if a == arm and substr in task_prompt:
                score = val
                break
        detail = json.dumps({"judge_score": score, "rubric_version": "1.0"})
        return score >= 0.6, score, detail


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    # Don't require real CC credentials, and don't mutate the process env.
    monkeypatch.setattr(runner_mod, "prepare_bare_config_dir", lambda run_dir: run_dir / "cfg")
    monkeypatch.setattr(runner_mod, "scrub_nested_cc_env", lambda: [])


async def _run(invoker, scorer, tmp_path):
    return await run_skill_replay(
        skill_name="voice-master",
        old_content="OLDBODY",
        new_content="NEWBODY",
        tasks_path=_FIXTURE,
        config=_CFG,
        invoker=invoker,
        scorer=scorer,
        verify_prod=False,
        allow_repo_tasks=True,
        run_root=tmp_path,
    )


async def test_net_positive_when_new_improves_with_zero_regressions(tmp_path):
    scorer = _FakeScorer({("Task 1", "new"): 0.9, ("Task 2", "new"): 0.9})
    report = await _run(_FakeInvoker(), scorer, tmp_path)
    assert report.verdict.verdict == VERDICT_NET_POSITIVE
    assert report.verdict.n_regressions == 0
    assert report.verdict.n_improvements == 2
    assert report.verdict.n_complete == 5
    assert len(report.pairs) == 5


async def test_regression_when_new_loses_a_task(tmp_path):
    scorer = _FakeScorer({("Task 1", "old"): 0.9})  # OLD beats NEW on t1
    report = await _run(_FakeInvoker(), scorer, tmp_path)
    assert report.verdict.verdict == VERDICT_REGRESSION
    assert report.verdict.n_regressions == 1


async def test_all_ties_is_inconclusive(tmp_path):
    report = await _run(_FakeInvoker(), _FakeScorer({}), tmp_path)
    assert report.verdict.verdict == VERDICT_INCONCLUSIVE
    assert report.verdict.n_complete == 5


async def test_infra_skip_shrinks_n(tmp_path):
    # OLD arm of Task 1 errors → that pair is skipped; the other 4 still judge.
    invoker = _FakeInvoker(error_old_for=frozenset({"Task 1"}))
    report = await _run(invoker, _FakeScorer({}), tmp_path)
    assert len(report.pairs) == 5
    assert report.pairs[0].skipped is True
    assert report.verdict.n_complete == 4


async def test_report_carries_provenance(tmp_path):
    report = await _run(_FakeInvoker(), _FakeScorer({}), tmp_path)
    assert report.skill_name == "voice-master"
    assert report.task_set_version == "fixture_v1"
    assert report.rubric_name == "bench_task_success"
    assert report.task_file_sha256  # sha256 recorded for frozen-set provenance
