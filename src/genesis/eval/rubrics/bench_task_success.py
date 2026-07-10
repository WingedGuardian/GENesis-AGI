"""Bench task-success rubric — grades one A/B arm's output per task.

Used by ``genesis eval bench`` (WS-1 A3): each arm's final output is graded
INDEPENDENTLY against the task's ex-ante success criteria (per-arm absolute
judging — the two arms' scores are paired downstream by
``genesis.eval.stats.compute_score_winrate``; the judge never sees both
outputs, so no position-bias mitigation is needed).

UNCALIBRATED in v1 (user-decided 2026-07-09): every bench report and run row
carries ``judge_calibrated: false`` until ``run_calibration`` passes >= 80%
against a human-labeled golden set (``~/.genesis/eval/bench_judge_golden.jsonl``,
authored during the calibration follow-up). Bump ``version`` on ANY prompt
change — it is the series-break marker for cross-run comparability.
"""

from __future__ import annotations

from genesis.eval.rubrics import Rubric, register_rubric

_PROMPT = """You are grading whether an AI assistant completed a task.

Grade ONLY against the success criteria below. The criteria were written
BEFORE the attempt (ex-ante) — do not invent additional requirements, and do
not reward verbosity, style, confidence, or effort beyond what the criteria
demand. An output that satisfies every criterion tersely outscores one that
satisfies half of them eloquently.

THE TASK GIVEN TO THE ASSISTANT:
{task_prompt}

SUCCESS CRITERIA (ex-ante):
{expected}

THE ASSISTANT'S OUTPUT:
{actual}

Score from 0.0 to 1.0:
- 1.0 = every success criterion is clearly satisfied
- 0.5 = roughly half the criteria satisfied, or all satisfied only partially
- 0.0 = no criterion satisfied, output is off-task, empty, or fabricated

If a criterion requires specific facts, verify the output actually states
them — a plausible-sounding answer that omits or contradicts the required
facts scores low on that criterion.

Return ONLY a JSON object:
{{"score": <float>, "rationale": "<one or two sentences citing which criteria passed/failed>"}}
"""

BENCH_TASK_SUCCESS = Rubric(
    name="bench_task_success",
    version="1.0.0",
    description=(
        "A/B bench (genesis eval bench): grades one arm's output against the "
        "task's ex-ante success criteria. Per-arm absolute judging; pairing "
        "and win-rate happen downstream. UNCALIBRATED until the bench golden "
        "set exists — consumers must surface judge_calibrated: false."
    ),
    prompt_template=_PROMPT,
    # Provisional until calibration; matches the other judge rubrics' 0.6.
    pass_threshold=0.6,
    extra_placeholders=("task_prompt",),
)

register_rubric(BENCH_TASK_SUCCESS)
