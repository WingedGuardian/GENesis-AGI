"""Tests for genesis.autonomy.verification — TaskVerifier."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from genesis.autonomy.types import CompletionArtifact
from genesis.autonomy.verification import TaskVerifier, _code_task_validator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_artifact(**overrides) -> CompletionArtifact:
    """Return a fully-populated artifact, with optional field overrides."""
    defaults = dict(
        task_id="task-001",
        what_attempted="Run lint checks",
        what_produced="Clean lint output",
        success=True,
        learnings="ruff is fast",
        error=None,
    )
    defaults.update(overrides)
    return CompletionArtifact(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def verifier() -> TaskVerifier:
    return TaskVerifier()


# ===========================================================================
# TestVerifyStructural
# ===========================================================================


class TestVerifyStructural:
    """Structural validation checks that always run."""

    def test_valid_artifact_passes(self, verifier: TaskVerifier) -> None:
        result = verifier.verify(_valid_artifact())
        assert result.passed is True
        assert result.errors == []
        assert result.warnings == []

    def test_empty_task_id_fails(self, verifier: TaskVerifier) -> None:
        result = verifier.verify(_valid_artifact(task_id=""))
        assert result.passed is False
        assert any("task_id" in e for e in result.errors)

    def test_empty_what_attempted_fails(self, verifier: TaskVerifier) -> None:
        result = verifier.verify(_valid_artifact(what_attempted=""))
        assert result.passed is False
        assert any("what_attempted" in e for e in result.errors)

    def test_empty_what_produced_fails(self, verifier: TaskVerifier) -> None:
        result = verifier.verify(_valid_artifact(what_produced=""))
        assert result.passed is False
        assert any("what_produced" in e for e in result.errors)

    def test_success_without_learnings_warns(self, verifier: TaskVerifier) -> None:
        result = verifier.verify(_valid_artifact(success=True, learnings=""))
        # Should warn but still pass — warnings are not errors.
        assert result.passed is True
        assert len(result.warnings) == 1
        assert "learnings" in result.warnings[0]

    def test_failure_without_error_warns(self, verifier: TaskVerifier) -> None:
        result = verifier.verify(_valid_artifact(success=False, error=None))
        assert result.passed is True
        assert len(result.warnings) == 1
        assert "error" in result.warnings[0].lower()

    def test_multiple_errors(self, verifier: TaskVerifier) -> None:
        result = verifier.verify(_valid_artifact(task_id="", what_attempted=""))
        assert result.passed is False
        assert len(result.errors) == 2
        error_text = " ".join(result.errors)
        assert "task_id" in error_text
        assert "what_attempted" in error_text


# ===========================================================================
# TestCustomValidators
# ===========================================================================


class TestCustomValidators:
    """Custom per-task-type validators."""

    def test_custom_validator_called(self, verifier: TaskVerifier) -> None:
        called_with: list[CompletionArtifact] = []

        def _validator(artifact: CompletionArtifact) -> list[str]:
            called_with.append(artifact)
            return []

        verifier.register_validator("code", _validator)
        art = _valid_artifact()
        verifier.verify(art, task_type="code")
        assert called_with == [art]

    def test_custom_validator_not_called_wrong_type(self, verifier: TaskVerifier) -> None:
        called = False

        def _validator(artifact: CompletionArtifact) -> list[str]:
            nonlocal called
            called = True
            return []

        verifier.register_validator("code", _validator)
        verifier.verify(_valid_artifact(), task_type="research")
        assert called is False

    def test_custom_validator_exception_handled(self, verifier: TaskVerifier) -> None:
        def _validator(artifact: CompletionArtifact) -> list[str]:
            raise RuntimeError("boom")

        verifier.register_validator("code", _validator)
        result = verifier.verify(_valid_artifact(), task_type="code")
        assert result.passed is False
        assert any("exception" in e.lower() for e in result.errors)

    def test_custom_validator_issues_added(self, verifier: TaskVerifier) -> None:
        def _validator(artifact: CompletionArtifact) -> list[str]:
            return ["bad thing"]

        verifier.register_validator("code", _validator)
        result = verifier.verify(_valid_artifact(), task_type="code")
        assert result.passed is False
        assert "bad thing" in result.errors


# ===========================================================================
# TestGroundwork
# ===========================================================================


class TestCodeTaskValidator:
    """Tests for the code-task verification gate (ruff + pytest)."""

    def test_code_validator_detects_ruff_failure(self) -> None:
        artifact = _valid_artifact()
        with patch("genesis.autonomy.verification.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout="E501 line too long"),  # ruff
                MagicMock(returncode=0, stdout=""),  # pytest
            ]
            errors = _code_task_validator(artifact)
        assert any("ruff" in e.lower() for e in errors)

    def test_code_validator_detects_pytest_failure(self) -> None:
        artifact = _valid_artifact()
        with patch("genesis.autonomy.verification.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),  # ruff passes
                MagicMock(returncode=1, stdout="1 failed"),  # pytest fails
            ]
            errors = _code_task_validator(artifact)
        assert any("pytest" in e.lower() for e in errors)

    def test_code_validator_clean(self) -> None:
        artifact = _valid_artifact()
        with patch("genesis.autonomy.verification.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            errors = _code_task_validator(artifact)
        assert errors == []

    def test_code_validator_handles_file_not_found(self) -> None:
        artifact = _valid_artifact()
        with patch("genesis.autonomy.verification.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ruff not found")
            errors = _code_task_validator(artifact)
        assert len(errors) > 0
        assert any("command not found" in e.lower() for e in errors)

    def test_code_validator_handles_timeout(self) -> None:
        import subprocess as _sp
        artifact = _valid_artifact()
        with patch("genesis.autonomy.verification.subprocess.run") as mock_run:
            mock_run.side_effect = _sp.TimeoutExpired(cmd=["ruff"], timeout=30)
            errors = _code_task_validator(artifact)
        assert len(errors) > 0
        assert any("timed out" in e.lower() for e in errors)

    def test_code_validator_uses_working_dir_from_outputs(self) -> None:
        artifact = _valid_artifact(outputs={"working_dir": "/tmp/myproject"})
        with patch("genesis.autonomy.verification.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            _code_task_validator(artifact)
        for call in mock_run.call_args_list:
            assert call.kwargs["cwd"] == "/tmp/myproject"
