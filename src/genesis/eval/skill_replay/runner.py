"""Paired OLD-vs-NEW replay orchestration for the skill-edit regression gate.

Per golden task, both arms (OLD content vs NEW content, pinned into the system
prompt) run CONCURRENTLY in the bench's bare-Claude isolation; tasks run
sequentially. Each arm's final text is judged per-arm-absolute against the
task's ex-ante criteria (shared ``bench_task_success`` rubric); the verdict is
computed here, downstream of the judge. Recommend-only — this runner mutates no
cognition and touches no DB (persistence + observation logging are the caller's
job).

Simpler than the A/B bench: neither arm uses memory MCP (a writing task is not a
recall task), so there is NO prod-DB snapshot, NO redirected MCP server, and NO
eval_events positive control — just a ProdDeltaProbe bracket as cheap
falsifiability. Reuses the bench's ``run_arm``/``judge_arm`` (harness-agnostic)
and its arm-isolation helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import shutil
import uuid
from pathlib import Path

from genesis.cc.types import CCModel, EffortLevel
from genesis.eval.bench.arms import prepare_bare_config_dir, scrub_nested_cc_env
from genesis.eval.bench.isolation import ProdDeltaProbe
from genesis.eval.bench.runner import judge_arm, run_arm
from genesis.eval.skill_replay.arms import build_skill_arm_invocation
from genesis.eval.skill_replay.types import (
    ARM_NEW,
    ARM_OLD,
    SkillReplayConfig,
    SkillReplayPair,
    SkillReplayReport,
)
from genesis.eval.skill_replay.verdict import compute_verdict

logger = logging.getLogger(__name__)

# v1 reuses the bench rubric — generic "does the output meet the ex-ante
# criteria" grading. A dedicated skill_replay_success rubric is a later refinement.
_RUBRIC_NAME = "bench_task_success"


class SkillReplayBusyError(RuntimeError):
    """Another skill-replay run holds the lock."""


def _acquire_lock():
    """Non-blocking skill-replay lock — separate from the bench lock so a replay
    and a bench run never starve each other."""
    lock_path = Path.home() / "tmp" / ".skill_replay.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — held for the run
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        fh.close()
        raise SkillReplayBusyError("another skill-replay run is already in progress") from e
    return fh


def _release_lock(fh) -> None:
    with contextlib.suppress(Exception):
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _build_judge_router(judge_provider: str | None):
    """The runtime `judge` call site's free-first chain (mirrors run_bench)."""
    from genesis.experimentation.standalone_router import (
        DEFAULT_JUDGE_PROVIDER,
        StandaloneLiteLLMRouter,
    )

    chain = [
        DEFAULT_JUDGE_PROVIDER,
        "openrouter-deepseek-v4",
        "openrouter-deepseek-v4-flash",
    ]
    if judge_provider:
        chain = [judge_provider, *[p for p in chain if p != judge_provider]]
    return StandaloneLiteLLMRouter(chain[0], fallback_providers=tuple(chain[1:]))


async def run_skill_replay(
    *,
    skill_name: str,
    old_content: str,
    new_content: str,
    tasks_path: Path | str,
    model: CCModel = CCModel.SONNET,
    effort: EffortLevel = EffortLevel.MEDIUM,
    config: SkillReplayConfig | None = None,
    limit: int | None = None,
    verify_prod: bool = True,
    invoker=None,
    scorer=None,
    router=None,
    run_root: Path | None = None,
    allow_repo_tasks: bool = False,
    judge_provider: str | None = None,
    keep_workdir: bool = False,
) -> SkillReplayReport:
    """Replay ``tasks_path`` against OLD vs NEW ``skill_name`` content.

    Returns a :class:`SkillReplayReport` with a recommend-only verdict.
    Persistence and observation logging are the CALLER's job (the MCP tool /
    CLI) — this runner touches no DB. Test seams: ``invoker`` (CCInvoker-shaped),
    ``scorer`` (LLMJudgeScorer-shaped), ``router`` (judge, only used to build a
    scorer when ``scorer`` is None), ``run_root``, ``allow_repo_tasks``.
    """
    from genesis.eval.bench.tasks import load_tasks
    from genesis.eval.rubrics import get_rubric

    config = config or SkillReplayConfig()

    removed = scrub_nested_cc_env()
    if removed:
        logger.info("skill_replay: scrubbed nested-CC env vars: %s", ", ".join(sorted(removed)))

    tasks, task_set_version, sha256 = load_tasks(tasks_path, allow_repo_path=allow_repo_tasks)
    if limit:
        tasks = tasks[:limit]

    run_id = uuid.uuid4().hex[:12]
    rubric = get_rubric(_RUBRIC_NAME)
    report = SkillReplayReport(
        run_id=run_id,
        skill_name=skill_name,
        model=str(model),
        effort=str(effort),
        task_set_version=task_set_version,
        task_file_sha256=sha256,
        rubric_name=rubric.name,
        rubric_version=rubric.version,
    )

    lock = _acquire_lock()
    run_dir = (run_root or Path.home() / "tmp" / "skill_replay") / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=True)

        probe = ProdDeltaProbe() if verify_prod else None
        if probe:
            probe.start()

        bare_cfg = prepare_bare_config_dir(run_dir)

        if invoker is None:
            from genesis.cc.invoker import CCInvoker

            invoker = CCInvoker()  # fresh, no callbacks → side-effect-free
        if scorer is None:
            from genesis.eval.scorers import LLMJudgeScorer

            scorer = LLMJudgeScorer(router=router or _build_judge_router(judge_provider))

        for i, task in enumerate(tasks, start=1):
            logger.info("skill_replay: task %d/%d %s (%s)", i, len(tasks), task.id, task.category)
            old_inv = build_skill_arm_invocation(
                task,
                run_dir,
                model,
                effort,
                skill_name=skill_name,
                skill_content=old_content,
                bare_config_dir=bare_cfg,
                run_id=run_id,
                arm_label=ARM_OLD,
            )
            new_inv = build_skill_arm_invocation(
                task,
                run_dir,
                model,
                effort,
                skill_name=skill_name,
                skill_content=new_content,
                bare_config_dir=bare_cfg,
                run_id=run_id,
                arm_label=ARM_NEW,
            )
            old_out, new_out = await asyncio.gather(
                run_arm(invoker, old_inv, task, ARM_OLD),
                run_arm(invoker, new_inv, task, ARM_NEW),
            )
            old_out = await judge_arm(scorer, task, old_out)
            new_out = await judge_arm(scorer, task, new_out)
            pair = SkillReplayPair(task=task, old=old_out, new=new_out)
            if pair.skipped:
                reasons = "; ".join(
                    f"{o.arm}: {o.skip_reason}" for o in (old_out, new_out) if o.skipped
                )
                logger.warning("skill_replay: pair %s SKIPPED (%s)", task.id, reasons)
            report.pairs.append(pair)

        if probe:
            report.prod_delta = probe.finish()
            if not report.prod_delta.get("clean", False):
                report.notes.append(
                    "PROD DELTA observed — attribute against concurrent server "
                    "activity before calling it a breach (the arms make zero writes)."
                )

        complete = [p for p in report.pairs if not p.skipped]
        report.verdict = compute_verdict(
            old_scores=[p.old.judge_score for p in complete],
            new_scores=[p.new.judge_score for p in complete],
            old_pass=[p.old.judge_passed for p in complete],
            new_pass=[p.new.judge_passed for p in complete],
            config=config,
        )
        if not complete:
            report.notes.append("no complete pairs — every task hit an infra skip")
        return report
    finally:
        _release_lock(lock)
        if not keep_workdir:
            shutil.rmtree(run_dir, ignore_errors=True)
