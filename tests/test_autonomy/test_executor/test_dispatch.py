"""Tests for genesis.autonomy.executor.dispatch (extracted helpers)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from genesis.autonomy.executor.dispatch import (
    _read_artifact,
    build_step_prompt,
    create_fixup_step,
    dominant_step_type,
    parse_step_output,
    select_deliverable_artifacts,
    synthesize_deliverable,
)
from genesis.autonomy.executor.types import StepResult


class TestBuildStepPrompt:
    def test_static_instructions_have_no_unresolved_context(self) -> None:
        prompt = build_step_prompt({"idx": 0, "description": "Research API"}, [])
        assert "{{" not in prompt
        assert "## Task\nNot provided\nPlanned step count: Not provided" in prompt
        assert "## Prior Step Results" not in prompt
        assert "Type: code" in prompt
        assert "Complexity: medium" in prompt

    @pytest.mark.parametrize("description", [None, ""])
    def test_missing_context_and_explicit_zero(self, description) -> None:
        prompt = build_step_prompt(
            {"idx": 4}, [], task_description=description, planned_step_count=0,
        )
        assert "## Task\nNot provided\nPlanned step count: 0" in prompt
        assert "## Step 4:" in prompt
        assert "4/0" not in prompt

    def test_values_are_verbatim_and_prior_results_stay_bounded(self) -> None:
        goal = 'Goal 日本語 {"key": "{{task_description}}"}'
        step = {"idx": 5, "description": "Fix {{step_description}}", "type": "code"}
        prior = [StepResult(idx=0, status="completed", result="a" * 201)]
        prompt = build_step_prompt(
            step, prior, "Use {{workaround}}", "Resource {{resources}}",
            task_description=goal, planned_step_count=2,
        )
        assert prompt.count(goal) == 1
        assert "Planned step count: 2" in prompt
        assert "## Step 5: Fix {{step_description}}" in prompt
        assert "5/2" not in prompt
        assert "Use {{workaround}}" in prompt
        assert "Resource {{resources}}" in prompt
        assert "a" * 200 in prompt and "a" * 201 not in prompt
        assert "**Notepad:**" in prompt

    def test_missing_template_still_renders_real_context(self, tmp_path, caplog) -> None:
        with patch(
            "genesis.autonomy.executor.dispatch._TASK_STEP_PROMPT_PATH",
            tmp_path / "missing.md",
        ):
            prompt = build_step_prompt(
                {"idx": 1, "type": "research", "description": "Find docs"}, [],
                task_description="Build a reader", planned_step_count=3,
            )
        assert "## Task\nBuild a reader\nPlanned step count: 3" in prompt
        assert "## Step 1: Find docs" in prompt
        assert "**Notepad:**" not in prompt
        assert "Failed to load step template" in caplog.text

    def test_includes_step_info(self) -> None:
        step = {"idx": 0, "description": "Research API", "type": "research", "complexity": "low"}
        prompt = build_step_prompt(step, [])

        assert "Step 0: Research API" in prompt
        assert "Type: research" in prompt
        assert "Complexity: low" in prompt

    def test_includes_prior_results(self) -> None:
        prior = [StepResult(idx=0, status="completed", result="Found docs")]
        step = {"idx": 1, "description": "Implement", "type": "code"}
        prompt = build_step_prompt(step, prior)

        assert "Prior Step Results" in prompt
        assert "Step 0: completed" in prompt

    def test_includes_workaround_context(self) -> None:
        step = {"idx": 0, "type": "code"}
        prompt = build_step_prompt(step, [], workaround="Try approach B")

        assert "Workaround Context" in prompt
        assert "Try approach B" in prompt

    def test_includes_resources(self) -> None:
        step = {"idx": 0, "type": "code"}
        prompt = build_step_prompt(
            step, [], resources="### Skill: research\nDo deep research.",
        )

        assert "Resources for This Step" in prompt
        assert "research" in prompt
        assert "Do deep research" in prompt

    def test_no_dynamic_resources_when_none(self) -> None:
        step = {"idx": 0, "type": "code"}
        prompt = build_step_prompt(step, [], resources=None)

        # The template mentions "Resources for This Step" in guidance text,
        # but the dynamic section header "## Resources for This Step" should not appear
        assert "## Resources for This Step" not in prompt


class TestParseStepOutput:
    def test_backtick_json(self) -> None:
        text = 'Some output\n```json\n{"status": "completed", "result": "done"}\n```'
        result = parse_step_output(text)
        assert result["status"] == "completed"
        assert result["result"] == "done"

    def test_inline_json(self) -> None:
        text = 'Thinking...\n{"status": "blocked", "blocker_description": "need creds"}'
        result = parse_step_output(text)
        assert result["status"] == "blocked"

    def test_empty_text(self) -> None:
        result = parse_step_output("")
        assert result["status"] == "completed"
        assert result["result"] == ""

    def test_no_json_falls_back(self) -> None:
        text = "Just plain text output with no JSON"
        result = parse_step_output(text)
        assert result["status"] == "completed"
        assert "Just plain text" in result["result"]


class TestReadArtifact:
    def test_reads_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "output.txt"
        f.write_text("hello world", encoding="utf-8")
        result = _read_artifact(str(f))
        assert "hello world" in result
        assert "```" in result

    def test_missing_file(self) -> None:
        result = _read_artifact("/nonexistent/file.txt")
        assert "does not exist" in result

    def test_directory_skipped(self, tmp_path: Path) -> None:
        result = _read_artifact(str(tmp_path))
        assert "not a regular file" in result

    def test_binary_file(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x00\x01\x02\xff" * 100)
        result = _read_artifact(str(f))
        assert "binary or unreadable" in result

    def test_large_file_truncated(self, tmp_path: Path) -> None:
        f = tmp_path / "large.txt"
        f.write_text("x" * 100_000, encoding="utf-8")
        result = _read_artifact(str(f))
        assert "Truncated" in result
        assert "100,000" in result


class TestSynthesizeDeliverable:
    def test_combines_completed_steps(self) -> None:
        results = [
            StepResult(idx=0, status="completed", result="Part A"),
            StepResult(idx=1, status="failed", result="Oops"),
            StepResult(idx=2, status="completed", result="Part C"),
        ]
        text = synthesize_deliverable(results)
        assert "Part A" in text
        assert "Oops" not in text
        assert "Part C" in text

    def test_empty_results(self) -> None:
        assert synthesize_deliverable([]) == ""

    def test_includes_artifacts(self, tmp_path: Path) -> None:
        f = tmp_path / "result.txt"
        f.write_text("file content here", encoding="utf-8")
        results = [
            StepResult(
                idx=0, status="completed", result="Built output",
                artifacts=[str(f)],
            ),
        ]
        text = synthesize_deliverable(results)
        assert "Built output" in text
        assert "file content here" in text
        assert "Artifact:" in text

    def test_missing_artifact_included(self) -> None:
        results = [
            StepResult(
                idx=0, status="completed", result="Done",
                artifacts=["/nonexistent/path.txt"],
            ),
        ]
        text = synthesize_deliverable(results)
        assert "Done" in text
        assert "does not exist" in text

    def test_no_artifacts(self) -> None:
        results = [
            StepResult(idx=0, status="completed", result="No files"),
        ]
        text = synthesize_deliverable(results)
        assert "No files" in text
        assert "Artifact" not in text


class TestDominantStepType:
    def test_single_type(self) -> None:
        steps = [{"type": "code"}, {"type": "code"}, {"type": "research"}]
        assert dominant_step_type(steps) == "code"

    def test_empty_steps(self) -> None:
        assert dominant_step_type([]) == "code"

    def test_defaults_to_code(self) -> None:
        steps = [{}]
        assert dominant_step_type(steps) == "code"


@dataclass
class FakeVerifyResult:
    fresh_eyes_feedback: str | None = None
    adversarial_feedback: str | None = None
    programmatic_issues: list[str] | None = None

    def __post_init__(self):
        if self.programmatic_issues is None:
            self.programmatic_issues = []


class TestCreateFixupStep:
    def test_includes_all_feedback(self) -> None:
        verify = FakeVerifyResult(
            fresh_eyes_feedback="Missing error handling",
            adversarial_feedback="Edge case not covered",
            programmatic_issues=["lint failure"],
        )
        fixup = create_fixup_step(verify, 5)

        assert fixup["idx"] == 5
        assert fixup["type"] == "code"
        assert "Missing error handling" in fixup["description"]
        assert "Edge case not covered" in fixup["description"]
        assert "lint failure" in fixup["description"]

    def test_handles_no_feedback(self) -> None:
        verify = FakeVerifyResult()
        fixup = create_fixup_step(verify, 3)

        assert fixup["idx"] == 3
        assert "Address review feedback" in fixup["description"]


class TestSelectDeliverableArtifacts:
    """Split a deliverable task's artifacts into (rendered files, qa summaries)."""

    def test_splits_rendered_and_qa(self) -> None:
        results = [
            StepResult(
                idx=0, status="completed", result="...",
                artifacts=["/o/report.pdf", "/o/qa_summary.md"],
            ),
        ]
        rendered, qa = select_deliverable_artifacts(results)
        assert rendered == ["/o/report.pdf"]
        assert qa == ["/o/qa_summary.md"]

    def test_ignores_authoring_and_unrelated_files(self) -> None:
        # draft.md / notes.txt are authoring artifacts, never the deliverable.
        results = [
            StepResult(
                idx=0, status="completed", result="x",
                artifacts=["/o/notes.txt", "/o/draft.md"],
            ),
        ]
        rendered, qa = select_deliverable_artifacts(results)
        assert rendered == []
        assert qa == []

    def test_collects_across_steps_and_formats(self) -> None:
        results = [
            StepResult(idx=0, status="completed", result="a", artifacts=["/o/data.csv"]),
            StepResult(
                idx=1, status="completed", result="b",
                artifacts=["/o/deck.pptx", "/o/qa_summary.md"],
            ),
        ]
        rendered, qa = select_deliverable_artifacts(results)
        assert set(rendered) == {"/o/data.csv", "/o/deck.pptx"}
        assert qa == ["/o/qa_summary.md"]

    def test_empty(self) -> None:
        rendered, qa = select_deliverable_artifacts([])
        assert rendered == []
        assert qa == []

    def test_qa_summary_nonmd_routes_to_qa_not_rendered(self) -> None:
        # An HTML-format deliverable emits qa_summary.html; it must NOT be
        # mistaken for the rendered deliverable.
        results = [
            StepResult(
                idx=0, status="completed", result="x",
                artifacts=["/o/report.html", "/o/qa_summary.html"],
            ),
        ]
        rendered, qa = select_deliverable_artifacts(results)
        assert rendered == ["/o/report.html"]
        assert qa == ["/o/qa_summary.html"]
