"""Tests for the executor plan-review prompt (autonomy/executor/review.py).

idx 23: the "Do NOT flag gaps in these areas" list had no carve-out for tasks
whose requirements explicitly demand those behaviors (a task that *requires* a
specific timeout/retry/escalation policy would have its genuine gap suppressed).
"""

from __future__ import annotations

from genesis.autonomy.executor.review import TaskReviewer


def _prompt() -> str:
    return TaskReviewer._build_plan_review_prompt(
        plan_content="1. Do the thing.",
        task_description="Do the thing with a specific 30s timeout.",
    )


def test_do_not_flag_list_has_requirements_carveout():
    """The 'Do NOT flag' lead-in must exempt requirements that demand the behavior."""
    prompt = _prompt()
    assert "unless the task requirements explicitly specify such behavior" in prompt


def test_closing_infrastructure_line_has_requirements_carveout():
    """The closing 'Do not flag infrastructure concerns' line carries the same carve-out."""
    prompt = _prompt()
    # The closing instruction should not be an unconditional suppression either.
    assert "Do not flag infrastructure concerns the executor already handles" in prompt
    idx = prompt.rindex("Do not flag infrastructure concerns the executor already handles")
    tail = prompt[idx:]
    assert "unless" in tail
