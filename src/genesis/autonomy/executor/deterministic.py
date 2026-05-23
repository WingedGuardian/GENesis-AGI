"""Deterministic step executor — runs shell commands without a CC session.

Inspired by Archon's separation of deterministic vs AI nodes.  Steps of
type bash/test/git execute a shell command directly via asyncio subprocess.
No LLM inference, no cost, near-instant.

Safety guardrails block obviously destructive command patterns.
No timeout is applied — the subprocess runs to completion.  If a
subprocess hangs, cancel the task via ``cancel_task(task_id)``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from genesis.autonomy.executor.types import StepResult, StepType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety guardrails — block destructive command patterns
# ---------------------------------------------------------------------------

_BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|--force)\b"),  # rm -rf / rm --force
    re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b"),             # rm -fr
    re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*--force\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+.*of=/dev/\b"),
    re.compile(r":>\s*/"),                                      # truncate file
    re.compile(r">\s*/dev/s"),                                  # redirect to device
    re.compile(r"\bkillall\b"),
    re.compile(r"\bpkill\s+-9\b"),
    re.compile(r"\bchmod\s+-R\s+777\b"),
]


def validate_command(command: str) -> str | None:
    """Check *command* against safety guardrails.

    Returns ``None`` if the command is safe, or a human-readable reason
    string if it should be blocked.
    """
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(command):
            return f"Command blocked by safety guardrail: matches pattern {pattern.pattern!r}"
    return None


# ---------------------------------------------------------------------------
# Deterministic execution
# ---------------------------------------------------------------------------


async def execute_deterministic_step(
    step: dict,
    *,
    worktree_path: Path | None = None,
    prior_results: list[StepResult] | None = None,
) -> StepResult:
    """Execute a deterministic step by running its ``command`` field.

    Returns a :class:`StepResult` with ``cost_usd=0.0`` and
    ``model_used="deterministic"``.  Exit code 0 → completed,
    non-zero → failed.
    """
    idx = step.get("idx", 0)
    command = step.get("command", "")
    step_type_str = step.get("type", "bash")

    if not command:
        return StepResult(
            idx=idx,
            status="failed",
            result="Deterministic step has no 'command' field",
            model_used="deterministic",
            blocker_description="Missing command for deterministic step",
        )

    # Safety check
    blocked = validate_command(command)
    if blocked:
        logger.warning("Deterministic step %d blocked: %s", idx, blocked)
        return StepResult(
            idx=idx,
            status="failed",
            result=blocked,
            model_used="deterministic",
            blocker_description=blocked,
        )

    # Resolve working directory
    try:
        step_type = StepType(step_type_str)
    except ValueError:
        step_type = StepType.BASH

    cwd = worktree_path or Path.cwd()

    logger.info(
        "Deterministic step %d [%s]: %s (cwd=%s)",
        idx, step_type.value, command, cwd,
    )

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
    except OSError as exc:
        duration = time.monotonic() - start
        return StepResult(
            idx=idx,
            status="failed",
            result=f"Failed to start subprocess: {exc}",
            model_used="deterministic",
            duration_s=duration,
            blocker_description=str(exc),
        )

    duration = time.monotonic() - start
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    # Cap output to avoid memory bloat in result_json
    max_output = 50_000
    if len(stdout) > max_output:
        stdout = stdout[:max_output] + f"\n... (truncated, {len(stdout_bytes)} bytes total)"
    if len(stderr) > max_output:
        stderr = stderr[:max_output] + f"\n... (truncated, {len(stderr_bytes)} bytes total)"

    result_text = ""
    if stdout:
        result_text += f"STDOUT:\n{stdout}"
    if stderr:
        if result_text:
            result_text += "\n"
        result_text += f"STDERR:\n{stderr}"
    if not result_text:
        result_text = "(no output)"

    if proc.returncode == 0:
        logger.info(
            "Deterministic step %d completed in %.1fs",
            idx, duration,
        )
        return StepResult(
            idx=idx,
            status="completed",
            result=result_text,
            cost_usd=0.0,
            model_used="deterministic",
            duration_s=duration,
        )
    else:
        logger.warning(
            "Deterministic step %d failed (exit %d) in %.1fs",
            idx, proc.returncode, duration,
        )
        return StepResult(
            idx=idx,
            status="failed",
            result=result_text,
            cost_usd=0.0,
            model_used="deterministic",
            duration_s=duration,
            blocker_description=(
                f"Command exited with code {proc.returncode}. "
                f"stderr: {stderr[:500]}" if stderr else
                f"Command exited with code {proc.returncode}"
            ),
        )
