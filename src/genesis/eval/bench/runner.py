"""Paired A/B orchestration for ``genesis eval bench``.

Per task: both arms run CONCURRENTLY (independent sandboxes; only the Genesis
arm has MCP, against the run's DB snapshot), tasks sequentially. Each arm's
final text is judged per-arm-absolute against the task's ex-ante criteria;
pairing and win-rates happen here, downstream of the judge.

Skip semantics (gauntlet's "infra ≠ quality" rule): a CC error/timeout, an
empty output, or a judge call/parse failure skips the WHOLE pair — paired
stats need complete pairs; infra failures shrink N, never tilt the comparison.

Isolation invariants enforced EVERY run, not just in E2E:
  - ProdDeltaProbe brackets the arm executions (finishing BEFORE result
    persistence, so legitimate eval_runs writes stay out of the window).
  - Positive control: the snapshot's eval_events count MUST grow while the
    Genesis arm runs (recall emits J-9 events through the redirected server).
    No growth = the arm silently degraded to bare-plus-identity (MCP server
    never started) or the env redirect failed → the run is marked INVALID.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean

from genesis.cc.exceptions import CCError
from genesis.cc.types import CCModel, EffortLevel
from genesis.eval.bench.arms import (
    build_bare_arm_invocation,
    build_genesis_arm_invocation,
    prepare_bare_config_dir,
    scrub_nested_cc_env,
)
from genesis.eval.bench.isolation import (
    ProdDeltaProbe,
    count_snapshot_eval_events,
    generate_bench_mcp_config,
    snapshot_prod_db,
)
from genesis.eval.bench.types import (
    ARM_BARE,
    ARM_GENESIS,
    BenchArmOutcome,
    BenchPair,
    BenchReport,
    BenchTask,
)
from genesis.eval.types import EvalRunSummary, EvalTrigger, ScoredOutput, ScorerType, TaskCategory

logger = logging.getLogger(__name__)

_RUBRIC_NAME = "bench_task_success"
_DATASET = "bench_v1"
#: eval_results.actual_output cap — enough for any real answer, bounded
#: against a runaway transcript-sized output.
_OUTPUT_PERSIST_CAP = 20_000


class BenchBusyError(RuntimeError):
    """Another bench run holds the lock — CLI maps this to exit code 3."""


def _acquire_lock():
    """Non-blocking global bench lock (gauntlet's advisory-flock pattern)."""
    lock_path = Path.home() / "tmp" / ".bench.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — held for the run
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        fh.close()
        raise BenchBusyError("another bench run is already in progress") from e
    return fh


def _release_lock(fh) -> None:
    with contextlib.suppress(Exception):
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


async def _run_arm(
    invoker,
    inv,
    task: BenchTask,
    arm: str,
) -> BenchArmOutcome:
    """Execute one arm; infra failures become skipped outcomes."""
    try:
        output = await invoker.run(inv)
    except CCError as e:
        return BenchArmOutcome(
            task_id=task.id, arm=arm, output_text="",
            skipped=True, skip_reason=f"infra: {type(e).__name__}: {e}",
        )
    if output.is_error:
        return BenchArmOutcome(
            task_id=task.id, arm=arm, output_text="",
            skipped=True,
            skip_reason=f"infra: cc error: {output.error_message or 'unknown'}",
        )
    if not (output.text or "").strip():
        # Silent-cap signature: successful-looking empty completion.
        return BenchArmOutcome(
            task_id=task.id, arm=arm, output_text="",
            skipped=True, skip_reason="infra: empty output",
        )
    if getattr(output, "downgraded", False):
        # Fairness violation: this arm ran a lower tier than requested (e.g.
        # a quota downgrade). The pair can't be compared — infra skip.
        return BenchArmOutcome(
            task_id=task.id, arm=arm, output_text="",
            model_used=output.model_used,
            skipped=True,
            skip_reason=(
                f"infra: model downgrade ({output.model_requested} → "
                f"{output.model_used}) — fairness requires identical models"
            ),
        )
    return BenchArmOutcome(
        task_id=task.id,
        arm=arm,
        output_text=output.text,
        model_used=output.model_used,
        duration_s=output.duration_ms / 1000.0,
        cost_usd=output.cost_usd,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
    )


async def _judge_arm(scorer, task: BenchTask, outcome: BenchArmOutcome) -> BenchArmOutcome:
    """Judge one arm's output. Judge infra failures skip the outcome."""
    if outcome.skipped:
        return outcome
    passed, score, detail = await scorer.score_async(
        actual=outcome.output_text,
        expected=task.expected,
        config={"rubric_name": _RUBRIC_NAME, "task_prompt": task.prompt},
    )
    try:
        detail_obj = json.loads(detail)
    except json.JSONDecodeError:
        detail_obj = {}
    if detail_obj.get("error"):
        return BenchArmOutcome(
            task_id=outcome.task_id, arm=outcome.arm,
            output_text=outcome.output_text, model_used=outcome.model_used,
            duration_s=outcome.duration_s, cost_usd=outcome.cost_usd,
            input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
            skipped=True, skip_reason=f"judge: {detail_obj['error']}",
        )
    return BenchArmOutcome(
        task_id=outcome.task_id, arm=outcome.arm,
        output_text=outcome.output_text, model_used=outcome.model_used,
        duration_s=outcome.duration_s, cost_usd=outcome.cost_usd,
        input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
        judge_passed=passed, judge_score=score, judge_detail=detail,
    )


def _arm_summary(
    report: BenchReport,
    arm: str,
    run_id: str,
    duration_s: float,
    extra_metadata: dict,
) -> EvalRunSummary:
    outcomes = [
        (p.bare if arm == ARM_BARE else p.genesis) for p in report.pairs
    ]
    results = [
        ScoredOutput(
            case_id=o.task_id,
            passed=o.judge_passed,
            score=o.judge_score,
            actual_output=(o.output_text or o.skip_reason)[:_OUTPUT_PERSIST_CAP],
            scorer_type=ScorerType.LLM_JUDGE,
            scorer_detail=o.judge_detail or o.skip_reason,
            latency_ms=o.duration_s * 1000.0,
            input_tokens=o.input_tokens,
            output_tokens=o.output_tokens,
            cost_usd=o.cost_usd,
            skipped=o.skipped,
        )
        for o in outcomes
    ]
    live = [o for o in outcomes if not o.skipped]
    scores = [o.judge_score for o in live]
    return EvalRunSummary(
        run_id=run_id,
        model_id=report.model,
        model_profile=f"bench:{arm}",
        dataset=_DATASET,
        trigger=EvalTrigger.MANUAL,
        task_category=TaskCategory.AGENTIC,
        total_cases=len(outcomes),
        passed_cases=sum(1 for o in live if o.judge_passed),
        failed_cases=sum(1 for o in live if not o.judge_passed),
        skipped_cases=sum(1 for o in outcomes if o.skipped),
        aggregate_score=fmean(scores) if scores else 0.0,
        scores={"mean_judge_score": fmean(scores)} if scores else {},
        metadata={
            "bench": True,
            "arm": arm,
            "judge_calibrated": False,
            "rubric": report.rubric_name,
            "rubric_version": report.rubric_version,
            "task_set_version": report.task_set_version,
            "task_file_sha256": report.task_file_sha256,
            "effort": report.effort,
            **extra_metadata,
        },
        duration_s=duration_s,
        results=results,
    )


async def run_bench(
    *,
    tasks_path: Path | str | None = None,
    model: CCModel = CCModel.SONNET,
    effort: EffortLevel = EffortLevel.MEDIUM,
    limit: int | None = None,
    task_id: str | None = None,
    epsilon: float = 0.05,
    db=None,
    keep_workdir: bool = False,
    verify_prod: bool = True,
    router=None,
    invoker=None,
    run_root: Path | None = None,
    allow_repo_tasks: bool = False,
) -> BenchReport:
    """Run the full paired bench. Returns the report (also written to disk).

    Test seams: ``invoker`` (a CCInvoker-shaped object), ``router`` (judge),
    ``run_root``, ``allow_repo_tasks`` (fixture loading only).
    """
    from genesis.env import genesis_home
    from genesis.eval.bench.tasks import DEFAULT_TASKS_PATH, load_tasks
    from genesis.eval.rubrics import get_rubric
    from genesis.eval.scorers import LLMJudgeScorer
    from genesis.eval.stats import compute_score_winrate, compute_winrate

    removed = scrub_nested_cc_env()
    if removed:
        logger.info("bench: scrubbed nested-CC env vars: %s", ", ".join(sorted(removed)))

    tasks, task_set_version, sha256 = load_tasks(
        tasks_path or DEFAULT_TASKS_PATH, allow_repo_path=allow_repo_tasks,
    )
    if task_id:
        tasks = [t for t in tasks if t.id == task_id]
        if not tasks:
            from genesis.eval.bench.tasks import TaskFileError

            raise TaskFileError(f"no task with id {task_id!r}")
    if limit:
        tasks = tasks[:limit]

    run_id = uuid.uuid4().hex[:12]
    rubric = get_rubric(_RUBRIC_NAME)
    report = BenchReport(
        run_id=run_id,
        model=str(model),
        effort=str(effort),
        task_set_version=task_set_version,
        task_file_sha256=sha256,
        rubric_name=rubric.name,
        rubric_version=rubric.version,
        judge_calibrated=False,
    )

    lock = _acquire_lock()
    started = datetime.now(UTC)
    run_dir = (run_root or Path.home() / "tmp" / "bench") / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)

        probe = ProdDeltaProbe() if verify_prod else None
        if probe:
            probe.start()

        snapshot = await asyncio.to_thread(snapshot_prod_db, run_dir)
        report.notes.append(
            f"snapshot_created_at={started.isoformat()} (SQLite frozen; "
            "Qdrant reads live prod — mixed-freshness recall possible)"
        )
        events_before = count_snapshot_eval_events(snapshot)
        mcp_config_path = generate_bench_mcp_config(run_dir, snapshot)
        bare_cfg = prepare_bare_config_dir(run_dir)

        if invoker is None:
            from genesis.cc.invoker import CCInvoker

            invoker = CCInvoker()  # fresh, no callbacks → side-effect-free

        if router is None:
            from genesis.experimentation.standalone_router import (
                DEFAULT_JUDGE_PROVIDER,
                StandaloneLiteLLMRouter,
            )

            # Mirror the runtime `judge` call site's chain (free NIM first,
            # then paid OpenRouter) — one attempt per provider, so a hanging
            # provider costs one timeout, not the whole judge budget.
            router = StandaloneLiteLLMRouter(
                DEFAULT_JUDGE_PROVIDER,
                fallback_providers=(
                    "openrouter-deepseek-v4",
                    "openrouter-deepseek-v4-flash",
                ),
            )
        scorer = LLMJudgeScorer(router=router)

        genesis_ran = False  # pre-judge: did the Genesis ARM itself succeed?
        for i, task in enumerate(tasks, start=1):
            logger.info("bench: task %d/%d %s (%s)", i, len(tasks), task.id, task.category)
            bare_inv = build_bare_arm_invocation(
                task, run_dir, model, effort, bare_cfg, run_id,
            )
            genesis_inv = build_genesis_arm_invocation(
                task, run_dir, model, effort, mcp_config_path, run_id,
            )
            bare_out, genesis_out = await asyncio.gather(
                _run_arm(invoker, bare_inv, task, ARM_BARE),
                _run_arm(invoker, genesis_inv, task, ARM_GENESIS),
            )
            # Captured BEFORE judging — a judge failure must not mask the
            # arm-degradation positive control below.
            genesis_ran = genesis_ran or not genesis_out.skipped
            bare_out = await _judge_arm(scorer, task, bare_out)
            genesis_out = await _judge_arm(scorer, task, genesis_out)
            pair = BenchPair(task=task, bare=bare_out, genesis=genesis_out)
            if pair.skipped:
                reasons = "; ".join(
                    f"{o.arm}: {o.skip_reason}"
                    for o in (bare_out, genesis_out) if o.skipped
                )
                logger.warning("bench: pair %s SKIPPED (%s)", task.id, reasons)
            report.pairs.append(pair)

        # Positive control — the Genesis arm must have exercised its memory
        # server (recall emits J-9 events into the SNAPSHOT). Uses the
        # PRE-judge ran state accumulated in the loop, so an all-infra-skip
        # run doesn't false-alarm and a judge outage doesn't mask this.
        events_after = count_snapshot_eval_events(snapshot)
        if genesis_ran and events_after <= events_before:
            report.notes.append(
                "INVALID RUN (arm_degraded): snapshot eval_events did not grow "
                f"({events_before} → {events_after}) — the Genesis arm ran "
                "without a working memory server (MCP startup failure or env "
                "redirect failure). Comparison is bare-vs-bare+identity."
            )
            logger.error("bench: %s", report.notes[-1])

        if probe:
            report.prod_delta = probe.finish()
            if not report.prod_delta.get("clean", False):
                # On a LIVE system the probe is evidence, not a verdict: the
                # server's ambient activity (session extraction, concurrent
                # recalls, observations) moves these counters on its own
                # (measured 2026-07-09: +17 episodic points in a 13-min
                # window with the bench making ZERO recalls). Attribute
                # before acting: the dispositive bench-side checks are the
                # snapshot positive control + the writebacks seam + tool
                # policy. A delta during a genuinely quiet window IS a breach.
                report.notes.append(
                    "PROD DELTA observed — requires attribution (live prod "
                    "has ambient writes; see prod_delta and compare against "
                    "concurrent server activity before calling it a breach)."
                )

        complete = [p for p in report.pairs if not p.skipped]
        if complete:
            bare_scores = [p.bare.judge_score for p in complete]
            genesis_scores = [p.genesis.judge_score for p in complete]
            report.score_winrate = compute_score_winrate(
                bare_scores, genesis_scores, epsilon=epsilon,
            )
            report.pass_winrate = compute_winrate(
                [p.bare.judge_passed for p in complete],
                [p.genesis.judge_passed for p in complete],
            )
        else:
            report.notes.append("no complete pairs — every task hit an infra skip")

        duration_s = (datetime.now(UTC) - started).total_seconds()

        # Report to disk FIRST — a multi-hour run must survive a locked DB.
        from genesis.eval.bench.report import write_report

        report_path = write_report(report, run_dir, genesis_home() / "output")
        logger.info("bench: report written to %s", report_path)

        if db is not None:
            try:
                from genesis.eval.db import insert_run, set_comparison_run

                invalid = any("INVALID RUN" in n for n in report.notes)
                extra = {
                    "epsilon": epsilon,
                    "prod_delta_clean": report.prod_delta.get("clean"),
                    "invalid": invalid,
                    "stats": {
                        "score_winrate": report.score_winrate,
                        "pass_winrate": report.pass_winrate,
                    },
                }
                control_id = await insert_run(
                    db,
                    _arm_summary(report, ARM_BARE, f"{run_id}-bare", duration_s, extra),
                )
                treatment_id = await insert_run(
                    db,
                    _arm_summary(
                        report, ARM_GENESIS, f"{run_id}-genesis", duration_s,
                        {**extra, "paired_run_id": control_id},
                    ),
                )
                await set_comparison_run(db, treatment_id, control_id)
                await db.commit()
                report.control_run_id = control_id
                report.treatment_run_id = treatment_id
            except Exception:
                # Non-fatal by design: the report is already on disk; replay
                # persistence from it rather than wasting the whole run.
                logger.exception(
                    "bench: persisting to eval_runs failed — report at %s "
                    "is the source of truth for replay", report_path,
                )
                report.notes.append("PERSISTENCE FAILED — see log; report on disk")

        return report
    finally:
        _release_lock(lock)
        if not keep_workdir:
            shutil.rmtree(run_dir, ignore_errors=True)
