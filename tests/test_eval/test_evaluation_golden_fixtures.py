"""Synthetic evaluation cases load and complete paired replay without live services."""

from dataclasses import dataclass
from pathlib import Path

import pytest

import genesis.eval.skill_replay.runner as runner_mod
from genesis.eval.bench.tasks import load_tasks
from genesis.eval.skill_replay.runner import run_skill_replay
from genesis.eval.skill_replay.types import SkillReplayConfig

FIXTURES = Path(__file__).parent / "skill_golden_fixtures"


@pytest.mark.parametrize("skill", ["evaluate", "user_evaluate"])
def test_evaluation_suite_contract(skill):
    tasks, version, digest = load_tasks(
        FIXTURES / f"{skill}_fixture.jsonl", allow_repo_path=True,
    )
    assert version == f"{skill}_synthetic_v1"
    assert len(tasks) == len({task.id for task in tasks}) == 6
    assert len(digest) == 64
    for task in tasks:
        assert task.category == "research"
        assert task.context and task.prompt and task.expected
        task_text = "\n".join((task.prompt, task.context, task.expected)).lower()
        assert not any(
            marker in task_text for marker in ("todo", "placeholder", "<replace")
        )


@dataclass
class FakeOutput:
    text: str = "Synthetic response"
    is_error: bool = False
    error_message: str = ""
    model_used: str = "fake-model"
    duration_ms: float = 1.0
    cost_usd: float = 0.0
    input_tokens: int = 1
    output_tokens: int = 1


class FakeInvoker:
    def __init__(self):
        self.calls = []

    async def run(self, invocation):
        self.calls.append(invocation)
        return FakeOutput()


class TieScorer:
    def __init__(self):
        self.calls = []

    async def score_async(self, *, actual, expected, config):
        self.calls.append((actual, expected, config))
        assert actual == "Synthetic response"
        assert expected
        return True, 0.8, '{"judge_score": 0.8, "rubric_version": "1.0"}'


@pytest.mark.parametrize("skill", ["evaluate", "user_evaluate"])
async def test_evaluation_suite_completes_paired_replay(skill, monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "prepare_bare_config_dir", lambda root: root / "cfg")
    monkeypatch.setattr(runner_mod, "scrub_nested_cc_env", lambda: [])
    monkeypatch.setattr(runner_mod, "_acquire_lock", lambda: None)
    monkeypatch.setattr(runner_mod, "_release_lock", lambda lock: None)
    invoker = FakeInvoker()
    scorer = TieScorer()
    report = await run_skill_replay(
        skill_name=skill,
        old_content="Frozen baseline skill",
        new_content="Frozen proposed skill",
        tasks_path=FIXTURES / f"{skill}_fixture.jsonl",
        config=SkillReplayConfig(min_pairs=6),
        invoker=invoker,
        scorer=scorer,
        verify_prod=False,
        allow_repo_tasks=True,
        run_root=tmp_path,
    )
    assert len(invoker.calls) == 12
    assert len(scorer.calls) == 12
    assert len(report.pairs) == 6
    assert all(not pair.skipped for pair in report.pairs)
    outcomes = [outcome for pair in report.pairs for outcome in (pair.old, pair.new)]
    assert all(outcome.judge_passed is True for outcome in outcomes)
    assert all(outcome.judge_score == 0.8 for outcome in outcomes)
    assert all('"rubric_version": "1.0"' in outcome.judge_detail for outcome in outcomes)
    assert report.verdict.n_complete == 6
    assert report.verdict.verdict == "inconclusive"
    assert report.verdict.n_improvements == report.verdict.n_regressions == 0
    assert report.task_set_version == f"{skill}_synthetic_v1"
