"""Orchestration + aggregation for the LongMemEval harness.

Per question: build a fresh ephemeral store, ingest the haystack ONCE, then run
each arm (query-mode x rerank) against the frozen store (recall write-backs are
suppressed via ``GENESIS_MEMORY_WRITEBACKS_OFF`` so arms don't contaminate each
other). Each arm's recalled memories feed the reader; the hypothesis is graded
by the gpt-4o judge. Results aggregate per-arm into an ``EvalRunSummary`` (one
persisted run per arm) with overall + per-question-type accuracy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from statistics import fmean
from typing import TYPE_CHECKING

from genesis.eval.longmemeval.answer import answer_question
from genesis.eval.longmemeval.client import DEFAULT_MODEL, build_client
from genesis.eval.longmemeval.ingest import ingest_haystack
from genesis.eval.longmemeval.judge import judge_answer
from genesis.eval.longmemeval.query import QueryArm, build_query
from genesis.eval.longmemeval.store import ephemeral_store
from genesis.eval.types import (
    EvalRunSummary,
    EvalTrigger,
    ScoredOutput,
    ScorerType,
    TaskCategory,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

    from genesis.eval.longmemeval.dataset import LongMemEvalInstance

logger = logging.getLogger("genesis.eval.longmemeval")

# gpt-4o-2024-08-06 pricing (USD per token).
_COST_IN = 2.50 / 1_000_000
_COST_OUT = 10.0 / 1_000_000

_DATASET = "longmemeval_oracle"
_WRITEBACKS_OFF_ENV = "GENESIS_MEMORY_WRITEBACKS_OFF"


@dataclass(frozen=True)
class Arm:
    """One evaluation arm: a query mode crossed with rerank on/off."""

    query_arm: QueryArm
    rerank: bool

    @property
    def label(self) -> str:
        suffix = "+rerank" if self.rerank else ""
        return f"{self.query_arm.value}{suffix}"


def default_arms() -> list[Arm]:
    """The four arms reported by default: {raw, keyword} x {rerank off, on}."""
    return [
        Arm(QueryArm.RAW, rerank=False),
        Arm(QueryArm.RAW, rerank=True),
        Arm(QueryArm.KEYWORD, rerank=False),
        Arm(QueryArm.KEYWORD, rerank=True),
    ]


@dataclass(frozen=True)
class QuestionArmResult:
    question_id: str
    question_type: str
    arm_label: str
    hypothesis: str
    judged_correct: bool
    evidence_recalled: bool
    judge_raw: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


def _cost(input_tokens: int, output_tokens: int) -> float:
    return input_tokens * _COST_IN + output_tokens * _COST_OUT


def build_run_summary(
    *,
    arm_label: str,
    model: str,
    dataset: str,
    results: Sequence[QuestionArmResult],
) -> EvalRunSummary:
    """Aggregate one arm's per-question results into a persistable summary."""
    total = len(results)
    passed = sum(1 for r in results if r.judged_correct)

    # Per-question-type accuracy.
    by_type: dict[str, list[int]] = {}
    for r in results:
        by_type.setdefault(r.question_type, []).append(1 if r.judged_correct else 0)
    scores: dict[str, float] = {qtype: round(fmean(vals), 4) for qtype, vals in by_type.items()}
    if results:
        scores["evidence_recall_rate"] = round(
            fmean(1 if r.evidence_recalled else 0 for r in results),
            4,
        )

    scored: list[ScoredOutput] = []
    for r in results:
        detail = json.dumps(
            {
                "question_type": r.question_type,
                "arm": r.arm_label,
                "evidence_recalled": r.evidence_recalled,
                "judge_raw": r.judge_raw,
            },
        )
        scored.append(
            ScoredOutput(
                case_id=r.question_id,
                passed=r.judged_correct,
                score=1.0 if r.judged_correct else 0.0,
                actual_output=r.hypothesis,
                scorer_type=ScorerType.LLM_JUDGE,
                scorer_detail=detail,
                latency_ms=r.latency_ms,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cost_usd=_cost(r.input_tokens, r.output_tokens),
            ),
        )

    return EvalRunSummary(
        run_id=uuid.uuid4().hex,
        model_id=model,
        model_profile=f"longmemeval:{arm_label}",
        dataset=dataset,
        trigger=EvalTrigger.MANUAL,
        task_category=TaskCategory.REASONING,
        total_cases=total,
        passed_cases=passed,
        failed_cases=total - passed,
        skipped_cases=0,
        aggregate_score=round(passed / total, 4) if total else 0.0,
        scores=scores,
        metadata={"arm": arm_label, "dataset": dataset},
        results=scored,
    )


async def run_question(
    instance: LongMemEvalInstance,
    *,
    client: object,
    arms: Sequence[Arm],
    k: int,
    embedding_provider: object | None = None,
    reranker: object | None = None,
) -> list[QuestionArmResult]:
    """Run every arm for one question against a fresh ephemeral store."""
    out: list[QuestionArmResult] = []
    async with ephemeral_store(
        embedding_provider=embedding_provider,
        reranker=reranker,
    ) as es:
        ingest = await ingest_haystack(es.store, instance)
        for arm in arms:
            t0 = time.monotonic()
            query = build_query(instance.question, arm.query_arm)
            hits = await es.retriever.recall(
                query,
                source="episodic",
                limit=k,
                rerank=arm.rerank,
            )
            recalled_ids = {h.memory_id for h in hits}
            evidence_recalled = bool(ingest.evidence_memory_ids & recalled_ids)
            memories = [h.content for h in hits]
            ans = answer_question(instance.question, memories, client=client)
            verdict = judge_answer(
                instance.question_type,
                instance.question,
                instance.answer,
                ans.hypothesis,
                abstention=instance.is_abstention,
                client=client,
            )
            out.append(
                QuestionArmResult(
                    question_id=instance.question_id,
                    question_type=instance.question_type,
                    arm_label=arm.label,
                    hypothesis=ans.hypothesis,
                    judged_correct=verdict.label,
                    evidence_recalled=evidence_recalled,
                    judge_raw=verdict.raw,
                    input_tokens=ans.input_tokens + verdict.input_tokens,
                    output_tokens=ans.output_tokens + verdict.output_tokens,
                    latency_ms=(time.monotonic() - t0) * 1000,
                ),
            )
    return out


async def run_longmemeval(
    instances: Sequence[LongMemEvalInstance],
    *,
    db: aiosqlite.Connection | None = None,
    arms: Sequence[Arm] | None = None,
    k: int = 10,
    concurrency: int = 4,
    client: object | None = None,
    embedding_provider: object | None = None,
    reranker: object | None = None,
) -> dict[str, EvalRunSummary]:
    """Run the full harness over ``instances``; return per-arm summaries.

    Sets ``GENESIS_MEMORY_WRITEBACKS_OFF`` so multi-arm recall against a shared
    per-question store cannot re-rank memories across arms. Persists one run per
    arm when ``db`` is provided.
    """
    os.environ.setdefault(_WRITEBACKS_OFF_ENV, "1")
    arms = list(arms) if arms is not None else default_arms()
    client = client or build_client()

    # No silent caps: if rerank arms are requested but no enabled reranker is
    # wired, those arms silently equal their non-rerank twins — say so loudly.
    wants_rerank = any(a.rerank for a in arms)
    reranker_enabled = bool(reranker) and bool(getattr(reranker, "enabled", False))
    if wants_rerank and not reranker_enabled:
        logger.warning(
            "rerank arms requested but no enabled reranker wired; those arms "
            "will produce identical results to their non-rerank twins",
        )

    sem = asyncio.Semaphore(concurrency)

    async def _one(inst: LongMemEvalInstance) -> list[QuestionArmResult]:
        async with sem:
            try:
                return await run_question(
                    inst,
                    client=client,
                    arms=arms,
                    k=k,
                    embedding_provider=embedding_provider,
                    reranker=reranker,
                )
            except Exception:
                logger.exception("question %s failed; skipping", inst.question_id)
                return []

    gathered = await asyncio.gather(*[_one(i) for i in instances])

    # Regroup by arm.
    by_arm: dict[str, list[QuestionArmResult]] = {a.label: [] for a in arms}
    for question_results in gathered:
        for r in question_results:
            by_arm.setdefault(r.arm_label, []).append(r)

    summaries: dict[str, EvalRunSummary] = {}
    for arm in arms:
        summary = build_run_summary(
            arm_label=arm.label,
            model=DEFAULT_MODEL,
            dataset=_DATASET,
            results=by_arm.get(arm.label, []),
        )
        summaries[arm.label] = summary
        if db is not None:
            from genesis.eval.db import insert_run

            await insert_run(db, summary)
    return summaries
