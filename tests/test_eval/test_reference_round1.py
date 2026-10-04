"""Offline regression population for the reference repair's three defect classes."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.eval import calibration
from genesis.eval.rubrics import get_rubric, list_rubrics
from genesis.eval.scorers import LLMJudgeScorer
from genesis.routing.types import RoutingResult


def _write(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return path


def _reference(rubric):
    return {
        "id": "one",
        "actual": "Observation",
        "expected": "",
        "user_passed": True,
        "scorer_config": {
            "rubric_name": rubric.name,
            **dict.fromkeys(rubric.extra_placeholders, "Context"),
        },
        "reference_provenance": {
            "label_source": "human",
            "reviewer": "synthetic",
            "rubric_version": rubric.version,
        },
    }


@pytest.mark.parametrize("rubric_name", [rubric.name for rubric in list_rubrics()])
@pytest.mark.parametrize("variation", ["same", "label", "metadata", "expected", "context"])
async def test_question_identity(tmp_path, rubric_name, variation):
    rubric = get_rubric(rubric_name)
    first = _reference(rubric)
    second = copy.deepcopy(first)
    second["id"] = "two"
    if variation == "label":
        second["user_passed"] = False
    elif variation == "metadata":
        second["reference_provenance"]["reviewer"] = "other"
        second["scorer_config"]["unused"] = "irrelevant"
    elif variation == "expected":
        second["expected"] = "Different reference"
    elif variation == "context":
        # Exercise every declared context component separately, without assuming its count.
        for name in rubric.extra_placeholders:
            distinct = copy.deepcopy(second)
            distinct["scorer_config"][name] = "Different context"
            path = _write(tmp_path / "references.jsonl", [first, distinct])
            assert len(calibration._load_golden_set(path, strict_rubric=rubric)) == 2
        return
    path = _write(tmp_path / "references.jsonl", [first, second])
    if variation == "expected":
        # Independent oracle: use the actual scorer's messages, not the validator's key.
        oracle = AsyncMock()
        oracle.route_call.return_value = RoutingResult(
            success=True, call_site_id="judge", content='{"score":0.9,"rationale":"Synthetic"}',
            provider_used="stub", model_id="stub",
        )
        scorer = LLMJudgeScorer(router=oracle)
        for row in (first, second):
            await scorer.score_async(row["actual"], row["expected"], row["scorer_config"])
        messages = [call.kwargs["messages"] for call in oracle.route_call.await_args_list]
        if messages[0] != messages[1]:
            assert len(calibration._load_golden_set(path, strict_rubric=rubric)) == 2
        else:
            router = AsyncMock()
            with pytest.raises(ValueError, match="duplicate grading question"):
                await calibration.run_calibration(
                    rubric=rubric, golden_set_path=path, router=router, strict_references=True,
                )
            router.route_call.assert_not_called()
    else:
        router = AsyncMock()
        with pytest.raises(ValueError, match="duplicate grading question"):
            await calibration.run_calibration(
                rubric=rubric, golden_set_path=path, router=router, strict_references=True
            )
        router.route_call.assert_not_called()
        assert len(calibration._load_golden_set(path)) == 2  # legacy compatibility


@pytest.mark.parametrize("failure", ["empty", "all_failed"])
async def test_empty_generation_can_retry(tmp_path, monkeypatch, failure):
    from genesis.eval import reflection_golden_set as generator

    real_connect = aiosqlite.connect
    monkeypatch.setattr(
        generator.aiosqlite, "connect", lambda *a, **k: real_connect(tmp_path / "source.db")
    )
    observation = dict(
        id="one", content="Observation", created_at="2026-10-02", priority=1, retrieved_count=0
    )
    sample = AsyncMock(return_value=[] if failure == "empty" else [observation])
    monkeypatch.setattr(generator, "_sample_observations", sample)
    monkeypatch.setattr(generator, "_get_session_context", AsyncMock(return_value="Context"))
    grade = AsyncMock(side_effect=ValueError("synthetic grading failure"))
    monkeypatch.setattr(generator, "_grade_observation", grade)
    output = tmp_path / "new-parent" / "draft.jsonl"
    with pytest.raises(ValueError, match="no successfully graded cases"):
        await generator.generate_golden_set(1, output)
    assert not output.parent.exists()
    sample.return_value = [observation]
    grade.side_effect = None
    grade.return_value = (0.9, "Synthetic", "stub-model")
    assert (await generator.generate_golden_set(1, output))["graded"] == 1
    with pytest.raises(FileExistsError):
        await generator.generate_golden_set(1, output)


@pytest.mark.parametrize("tool", ["experiment", "evo"])
@pytest.mark.parametrize(
    "population", ["historical", "draft", "both", "neither", "explicit_reference", "explicit_draft"]
)
async def test_actual_mcp_reference_defaults(tmp_path, monkeypatch, tool, population):
    import genesis.mcp.health.evo_run as evo_tool
    import genesis.mcp.health_mcp as health
    from genesis.eval import reflection_golden_set as generator
    from genesis.experimentation import evo, runner
    from genesis.experimentation.types import ArmResult, ExperimentResult
    from genesis.mcp.health.experiment_run import _impl_experiment_run

    reference = tmp_path / "historical.jsonl"
    draft = tmp_path / "draft.jsonl"
    explicit = tmp_path / "explicit.jsonl"
    row = _reference(get_rubric("reflection_quality"))
    if population in ("historical", "both", "explicit_reference", "explicit_draft"):
        _write(reference, [row])
    if population in ("draft", "both", "explicit_draft"):
        proposal = copy.deepcopy(row)
        proposal["proposed_passed"] = proposal.pop("user_passed")
        _write(draft, [proposal])
    _write(explicit, [row])
    before = {p: p.read_bytes() for p in (reference, draft, explicit) if p.exists()}
    monkeypatch.setattr(calibration, "DEFAULT_REFLECTION_REFERENCE", reference, raising=False)
    monkeypatch.setattr(generator, "DEFAULT_OUTPUT", draft)
    monkeypatch.setattr(health, "_service", SimpleNamespace(_db=object()), raising=False)
    captured = []

    async def evaluate(**kwargs):
        selected = kwargs["golden_set_path"]
        captured.append(selected)
        calibration._load_golden_set(selected)
        if tool == "evo":
            return evo.EvoResult(None, None, None, 1, 0, "Synthetic")
        arm = ArmResult("stub", [0.9], [True], 1, 0.9)
        return ExperimentResult("stub", arm, arm, {}, 1, 0, {})

    monkeypatch.setattr(runner, "run_reflection_experiment", evaluate)
    monkeypatch.setattr(evo, "run_evo", evaluate)
    monkeypatch.setattr(evo, "persist_evo_summary", AsyncMock(return_value="synthetic-run"))
    routers = [SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())]
    monkeypatch.setattr(evo_tool, "_build_routers", lambda *a: (*routers, object()))
    override = (
        str(explicit)
        if population == "explicit_reference"
        else str(draft)
        if population == "explicit_draft"
        else None
    )
    if tool == "experiment":
        result = await _impl_experiment_run(
            experiment_name="stub",
            control_prompt="a",
            treatment_prompt="b",
            golden_set_path=override,
        )
    else:
        result = await evo_tool._impl_evo_run(
            base_prompt="synthetic", golden_set_path=override, propose=False
        )
    if population in ("historical", "both", "explicit_reference"):
        assert result["status"] == "ok"
        assert captured == [explicit if override else reference]
    elif population == "explicit_draft":
        assert result["status"] == "error"
        assert "user_passed" in result["message"]
        assert captured == [draft]
    else:
        assert result["status"] == "error"
        assert str(reference) in result["message"]
        assert "draft" in result["message"] and "human" in result["message"]
        assert not captured
    assert all(p.read_bytes() == data for p, data in before.items())


def test_cli_reference_and_draft_defaults_are_separate():
    from genesis.eval import reflection_golden_set, run_reflection_calibration

    assert run_reflection_calibration.DEFAULT_GOLDEN == calibration.DEFAULT_REFLECTION_REFERENCE
    assert reflection_golden_set.DEFAULT_OUTPUT != calibration.DEFAULT_REFLECTION_REFERENCE


@pytest.mark.parametrize("mode", ["partial", "all_good", "competing_creation", "existing"])
async def test_generator_nonempty_and_exclusive_boundaries(tmp_path, monkeypatch, mode):
    from genesis.eval import reflection_golden_set as generator

    real_connect = aiosqlite.connect

    def connect(*args, **kwargs):
        return real_connect(tmp_path / "source.db")

    monkeypatch.setattr(generator.aiosqlite, "connect", connect)
    observations = [
        dict(
            id=str(i),
            content=f"Observation {i}",
            created_at="2026-10-02",
            priority=1,
            retrieved_count=0,
        )
        for i in range(2)
    ]
    sample = AsyncMock(return_value=observations)
    monkeypatch.setattr(generator, "_sample_observations", sample)
    monkeypatch.setattr(generator, "_get_session_context", AsyncMock(return_value="Context"))
    output = tmp_path / "draft.jsonl"
    calls = []

    async def grade(content, context):
        calls.append(content)
        if mode == "partial" and content == "Observation 1":
            raise ValueError("Synthetic failure")
        if mode == "competing_creation":
            output.write_text("competing writer")
        return 0.9, "Synthetic", "stub-model"

    monkeypatch.setattr(generator, "_grade_observation", grade)
    if mode == "existing":
        output.write_text("existing reference")
        with pytest.raises(FileExistsError):
            await generator.generate_golden_set(2, output)
        assert output.read_text() == "existing reference"
        sample.assert_not_called()
        assert not calls
    elif mode == "competing_creation":
        with pytest.raises(FileExistsError):
            await generator.generate_golden_set(2, output)
        assert output.read_text() == "competing writer"
    else:
        summary = await generator.generate_golden_set(2, output)
        assert summary["graded"] == (1 if mode == "partial" else 2)
        assert summary["errors"] == (1 if mode == "partial" else 0)
        rows = [
            json.loads(line) for line in output.read_text().splitlines() if not line.startswith("#")
        ]
        assert len(rows) == summary["graded"]
        assert all(
            "user_passed" not in row and row["reference_provenance"]["label_source"] == "model"
            for row in rows
        )
