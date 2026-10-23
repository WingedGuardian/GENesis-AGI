"""Bench task-success rubric — grades one A/B arm's output per task.

Used by ``genesis eval bench`` (WS-1 A3): each arm's final output is graded
INDEPENDENTLY against the task and its ex-ante success criteria (per-arm
absolute judging — the two arms' scores are paired downstream by
``genesis.eval.stats.compute_score_winrate``; the judge never sees both
outputs, so no position-bias mitigation is needed).

UNCALIBRATED: every bench report and run row carries ``judge_calibrated: false``
until ``run_calibration`` passes >= 80% against a human-labeled golden set
(``~/.genesis/eval/bench_judge_golden.jsonl``). Bump ``version`` on ANY prompt
change — it is the series-break marker for cross-run comparability.
"""

from __future__ import annotations

from genesis.eval.rubrics import Rubric, register_rubric

_PROMPT = """You are grading whether an AI assistant completed a task.

Identify the substantive deliverables explicitly required by the task or the ex-ante success criteria
below. The criteria were written BEFORE the attempt. Do not invent additional
requirements or treat optional details as required. Do not reward verbosity,
style, confidence, or effort as a substitute for completing the requested work.

Judge the requested stage: a requested progress update can complete that task;
an interim status message does not complete the eventual final deliverable.
A refusal or abstention counts as task success only when the task or ex-ante
criteria accept that outcome; judge those accepted criteria in that case.

For the judged stage/outcome, if the answer omits or contradicts any explicitly
required substantive deliverable, it is INCOMPLETE: score at most 0.5, even if
the rest is excellent. Keep partial credit below this ceiling; do not average
other strengths into a passing score. A required deliverable still counts when
it appears only in the task and is not repeated in the success criteria.

THE TASK GIVEN TO THE ASSISTANT:
{task_prompt}

SUCCESS CRITERIA (ex-ante):
{expected}

THE ASSISTANT'S OUTPUT:
{actual}

Score from 0.0 to 1.0:
- 1.0 = every required substantive deliverable and success criterion is clearly satisfied
- 0.5 = partial completion; any missing or contradicted required deliverable scores no higher
- 0.0 = no criterion satisfied, output is off-task, empty, or fabricated

If a criterion requires specific facts, verify the output actually states
them. Writing quality is separate from task completion.

Return ONLY a JSON object:
{{"score": <float>, "rationale": "<one or two sentences citing which criteria passed/failed>"}}
"""

BENCH_TASK_SUCCESS = Rubric(
    name="bench_task_success",
    version="1.1.0",
    description=(
        "A/B bench (genesis eval bench): grades one arm's task completion, "
        "including explicitly required deliverables and ex-ante criteria. "
        "Missing required work scores at most 0.5; numeric pass threshold 0.6. "
        "Per-arm absolute judging; pairing and win-rate happen downstream. "
        "UNCALIBRATED until the bench golden set calibrates the judge — "
        "consumers must surface judge_calibrated: false."
    ),
    prompt_template=_PROMPT,
    # Provisional until calibration; matches the other judge rubrics' 0.6.
    pass_threshold=0.6,
    extra_placeholders=("task_prompt",),
)

register_rubric(BENCH_TASK_SUCCESS)
