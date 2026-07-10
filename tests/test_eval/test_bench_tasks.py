"""Tests for the bench task loader, types, and rubric registration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from genesis.eval.bench.tasks import DEFAULT_TASKS_PATH, TaskFileError, load_tasks
from genesis.eval.bench.types import DEFAULT_TASK_TIMEOUT_S, BenchTask

FIXTURES = Path(__file__).parent / "bench_fixtures" / "synthetic_tasks.jsonl"


def _write_tasks(tmp_path: Path, lines: list[dict]) -> Path:
    p = tmp_path / "tasks.jsonl"
    p.write_text("\n".join(json.dumps(rec) for rec in lines), encoding="utf-8")
    return p


def _valid_task(**overrides) -> dict:
    rec = {
        "id": "t1",
        "category": "research",
        "prompt": "Do the thing.",
        "expected": "The thing is done.",
    }
    rec.update(overrides)
    return rec


def test_loads_synthetic_fixture():
    tasks, version, sha = load_tasks(FIXTURES, allow_repo_path=True)
    assert len(tasks) == 3
    assert version == "synthetic-fixtures-v1"
    assert sha == hashlib.sha256(FIXTURES.read_bytes()).hexdigest()
    by_id = {t.id: t for t in tasks}
    assert by_id["drafting_synth_1"].timeout_s == 600
    assert by_id["research_synth_1"].timeout_s == DEFAULT_TASK_TIMEOUT_S


def test_rejects_in_repo_path_without_escape_hatch():
    """The privacy gate: a task file inside the repo tree refuses to load."""
    with pytest.raises(TaskFileError, match="INSIDE the repo tree"):
        load_tasks(FIXTURES)


def test_default_path_is_outside_repo():
    from genesis.env import repo_root

    assert not str(DEFAULT_TASKS_PATH.resolve()).startswith(
        str(repo_root().resolve())
    )


def test_rejects_missing_expected(tmp_path):
    p = _write_tasks(tmp_path, [_valid_task(expected="")])
    with pytest.raises(TaskFileError, match="expected"):
        load_tasks(p)


def test_rejects_unknown_category(tmp_path):
    p = _write_tasks(tmp_path, [_valid_task(category="multi_session")])
    with pytest.raises(TaskFileError, match="unknown category"):
        load_tasks(p)


def test_rejects_duplicate_ids(tmp_path):
    p = _write_tasks(tmp_path, [_valid_task(), _valid_task()])
    with pytest.raises(TaskFileError, match="duplicate task id"):
        load_tasks(p)


def test_rejects_meta_after_first_line(tmp_path):
    p = _write_tasks(tmp_path, [_valid_task(), {"_meta": {"task_set_version": "x"}}])
    with pytest.raises(TaskFileError, match="first line"):
        load_tasks(p)


def test_rejects_bad_timeout(tmp_path):
    p = _write_tasks(tmp_path, [_valid_task(timeout_s=0)])
    with pytest.raises(TaskFileError, match="positive"):
        load_tasks(p)


def test_rejects_empty_file(tmp_path):
    p = tmp_path / "tasks.jsonl"
    p.write_text("", encoding="utf-8")
    with pytest.raises(TaskFileError, match="not found|no tasks"):
        load_tasks(p)


def test_sha256_stable_and_edit_visible(tmp_path):
    p = _write_tasks(tmp_path, [_valid_task()])
    _, _, sha_a = load_tasks(p)
    _, _, sha_b = load_tasks(p)
    assert sha_a == sha_b
    # Ex-ante freeze: editing `expected` after the fact changes the hash.
    p.write_text(json.dumps(_valid_task(expected="moved goalposts")), encoding="utf-8")
    _, _, sha_c = load_tasks(p)
    assert sha_c != sha_a


def test_rendered_prompt_includes_context():
    task = BenchTask(
        id="t", category="recall", prompt="Q?", expected="A.", context="notes",
    )
    rendered = task.rendered_prompt()
    assert rendered.startswith("Q?")
    assert "<context>" in rendered and "notes" in rendered
    bare = BenchTask(id="t", category="recall", prompt="Q?", expected="A.")
    assert bare.rendered_prompt() == "Q?"


def test_bench_rubric_registered_and_calibration_shaped():
    """The rubric is registered AND its template formats from a
    calibration-shaped case (proves run_calibration compatibility)."""
    from genesis.eval.rubrics import get_rubric, list_rubrics

    rubric = get_rubric("bench_task_success")
    assert rubric.version == "1.0.0"
    assert rubric.name in {r.name for r in list_rubrics()}
    assert rubric.extra_placeholders == ("task_prompt",)

    # calibration.py builds format kwargs as {actual, expected, **scorer_config
    # extras} — the exact shape LLMJudgeScorer.score_async uses.
    prompt = rubric.prompt_template.format(
        actual="the output",
        expected="the criteria",
        task_prompt="the task",
    )
    assert "the output" in prompt
    assert "the criteria" in prompt
    assert "the task" in prompt
    # The JSON contract survives str.format (double-brace escaping intact).
    assert '{"score":' in prompt


def test_calibration_uses_shared_standalone_router():
    """run_reflection_calibration was refactored onto the ONE shared shim
    (experimentation.standalone_router) — the cleanup its docstring tracked.
    Guards against a fourth inline copy reappearing."""
    import inspect

    from genesis.eval import run_reflection_calibration as cal_mod
    from genesis.experimentation.standalone_router import StandaloneLiteLLMRouter

    assert cal_mod.StandaloneLiteLLMRouter is StandaloneLiteLLMRouter
    src = inspect.getsource(cal_mod)
    assert "class _LiteLLMRouter" not in src
