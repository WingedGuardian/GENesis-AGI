"""Tests for genesis.autonomy.executor.dispatch (extracted helpers)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from genesis.autonomy.executor.dispatch import (
    _read_artifact,
    build_step_prompt,
    create_fixup_step,
    dominant_step_type,
    parse_step_output,
    synthesize_deliverable,
)
from genesis.autonomy.executor.types import StepResult


class TestBuildStepPrompt:
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
