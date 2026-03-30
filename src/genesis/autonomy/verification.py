"""Task verification gate — validates completion artifacts from dispatched tasks."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from genesis.autonomy.types import CompletionArtifact

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Outcome of verifying a CompletionArtifact."""

    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TaskVerifier:
    """Runs structural and custom checks on task completion artifacts."""

    def __init__(self) -> None:
        self._validators: dict[str, Callable[[CompletionArtifact], list[str]]] = {}

    def register_validator(
        self,
        task_type: str,
        validator: Callable[[CompletionArtifact], list[str]],
    ) -> None:
        """Register a custom validator for a task type.

        The validator receives a ``CompletionArtifact`` and returns a list of
        issue strings (empty list means the artifact passed).
        """
        self._validators[task_type] = validator
        logger.debug("Registered validator for task_type=%s", task_type)

    def verify(
        self,
        artifact: CompletionArtifact,
        *,
        task_type: str | None = None,
    ) -> VerificationResult:
        """Run all checks against *artifact* and return a :class:`VerificationResult`.

        Internal structural checks always run.  If *task_type* is provided and
        a custom validator is registered for it, that validator runs too.
        """
        result = VerificationResult()

        # --- Internal structural checks (always run) ---

        if not artifact.task_id:
            result.errors.append("task_id must be non-empty")

        if not artifact.what_attempted:
            result.errors.append("what_attempted must be non-empty")

        if not artifact.what_produced:
            result.errors.append("what_produced must be non-empty")

        if artifact.success and not artifact.learnings:
            result.warnings.append(
                "success is True but learnings is empty — consider recording what was learned"
            )

        if not artifact.success and not artifact.error:
            result.warnings.append(
                "success is False but error is empty — consider describing what went wrong"
            )

        # --- Custom validator (if registered) ---

        if task_type and task_type in self._validators:
            try:
                issues = self._validators[task_type](artifact)
                if issues:
                    result.errors.extend(issues)
            except Exception:
                logger.error(
                    "Custom validator for task_type=%s raised an exception",
                    task_type,
                    exc_info=True,
                )
                result.errors.append(
                    f"Custom validator for task_type={task_type!r} raised an exception"
                )

        # Final pass/fail determination — warnings are OK, errors are not.
        result.passed = len(result.errors) == 0

        logger.debug(
            "Verification for task_id=%s: passed=%s errors=%d warnings=%d",
            artifact.task_id,
            result.passed,
            len(result.errors),
            len(result.warnings),
        )

        return result


# ---------------------------------------------------------------------------
# Code-task verification — runs ruff + pytest against task output
# ---------------------------------------------------------------------------


def _code_task_validator(artifact: CompletionArtifact) -> list[str]:
    """Run ruff check and pytest to verify code task output."""

    from genesis.env import repo_root

    errors: list[str] = []
    working_dir = (
        artifact.outputs.get("working_dir", str(repo_root()))
        if artifact.outputs
        else str(repo_root())
    )

    for cmd, label, timeout in [
        (["ruff", "check", "."], "ruff check", 30),
        (["pytest", "-x", "--tb=short", "-q"], "pytest", 120),
    ]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir,
            )
            if result.returncode != 0:
                errors.append(f"{label} failed (exit {result.returncode}): {result.stdout[:500]}")
        except subprocess.TimeoutExpired:
            errors.append(f"{label} timed out after {timeout}s")
        except FileNotFoundError:
            errors.append(f"{label}: command not found (is {cmd[0]} installed?)")
        except OSError as exc:
            errors.append(f"{label} could not run: {exc}")

    return errors
