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
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
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
    #: |evidence ∩ recalled| / |evidence|; None when the question has no
    #: evidence turns (abstention) — excluded from means, never counted as 0.
    evidence_coverage: float | None = None
    query: str = ""
    recalled_ids: tuple[str, ...] = ()
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
    skipped_case_ids: Sequence[str] = (),
) -> EvalRunSummary:
    """Aggregate one arm's per-question results into a persistable summary.

    ``skipped_case_ids`` are questions that errored out (network/embedding/etc.)
    and were NOT attempted for this arm. They count toward ``total_cases`` and
    ``skipped_cases`` but NOT the denominator of ``aggregate_score`` — an infra
    failure shrinks N and is surfaced, never silently tilts accuracy or hides a
    provider outage (mirrors the A3 bench skip contract).
    """
    attempted = len(results)
    passed = sum(1 for r in results if r.judged_correct)
    skipped = len(skipped_case_ids)

    # Per-question-type accuracy (over attempted questions).
    by_type: dict[str, list[int]] = {}
    for r in results:
        by_type.setdefault(r.question_type, []).append(1 if r.judged_correct else 0)
    scores: dict[str, float] = {qtype: round(fmean(vals), 4) for qtype, vals in by_type.items()}
    if results:
        scores["evidence_recall_rate"] = round(
            fmean(1 if r.evidence_recalled else 0 for r in results),
            4,
        )
    # Mean evidence COVERAGE (any-hit recall is blind to partial recall on
    # multi-evidence questions — the key multi-session diagnostic).
    coverages = [r.evidence_coverage for r in results if r.evidence_coverage is not None]
    if coverages:
        scores["evidence_coverage_mean"] = round(fmean(coverages), 4)

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
    for qid in skipped_case_ids:
        scored.append(
            ScoredOutput(
                case_id=qid,
                passed=False,
                score=0.0,
                actual_output="",
                scorer_type=ScorerType.LLM_JUDGE,
                scorer_detail=json.dumps({"arm": arm_label, "skipped": True}),
                skipped=True,
            ),
        )

    return EvalRunSummary(
        run_id=uuid.uuid4().hex,
        model_id=model,
        model_profile=f"longmemeval:{arm_label}",
        dataset=dataset,
        trigger=EvalTrigger.MANUAL,
        task_category=TaskCategory.REASONING,
        total_cases=attempted + skipped,
        passed_cases=passed,
        failed_cases=attempted - passed,
        skipped_cases=skipped,
        aggregate_score=round(passed / attempted, 4) if attempted else 0.0,
        scores=scores,
        metadata={"arm": arm_label, "dataset": dataset, "skipped": skipped},
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
            evidence_ids = ingest.evidence_memory_ids
            evidence_recalled = bool(evidence_ids & recalled_ids)
            evidence_coverage = (
                len(evidence_ids & recalled_ids) / len(evidence_ids) if evidence_ids else None
            )
            memories = [h.content for h in hits]
            # answer_question / judge_answer use the SYNC OpenAI client, so run
            # them in a worker thread — a blocking create() on the event loop
            # would stall ALL concurrent questions and defeat `concurrency`.
            ans = await asyncio.to_thread(
                answer_question,
                instance.question,
                memories,
                client=client,
                question_type=instance.question_type,
                question_date=instance.question_date,
            )
            verdict = await asyncio.to_thread(
                lambda a=ans: judge_answer(
                    instance.question_type,
                    instance.question,
                    instance.answer,
                    a.hypothesis,
                    abstention=instance.is_abstention,
                    client=client,
                ),
            )
            out.append(
                QuestionArmResult(
                    question_id=instance.question_id,
                    question_type=instance.question_type,
                    arm_label=arm.label,
                    hypothesis=ans.hypothesis,
                    judged_correct=verdict.label,
                    evidence_recalled=evidence_recalled,
                    evidence_coverage=evidence_coverage,
                    query=query,
                    recalled_ids=tuple(h.memory_id for h in hits),
                    judge_raw=verdict.raw,
                    input_tokens=ans.input_tokens + verdict.input_tokens,
                    output_tokens=ans.output_tokens + verdict.output_tokens,
                    latency_ms=(time.monotonic() - t0) * 1000,
                ),
            )
    return out


def write_dump_jsonl(
    path: Path,
    results: Sequence[QuestionArmResult],
    *,
    skipped_case_ids: Sequence[str] = (),
) -> None:
    """Write one arm's per-question diagnostics as JSONL (one line/question).

    This is the failure-analysis artifact the summary rows can't provide:
    which memories each question actually recalled, the exact query, the
    hypothesis, and the judge verdict. Skipped (errored) questions get a
    ``{"skipped": true}`` marker line so the file accounts for the full N.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for r in results:
            fh.write(
                json.dumps(
                    {
                        "question_id": r.question_id,
                        "question_type": r.question_type,
                        "arm": r.arm_label,
                        "query": r.query,
                        "recalled_ids": list(r.recalled_ids),
                        "evidence_recalled": r.evidence_recalled,
                        "evidence_coverage": r.evidence_coverage,
                        "hypothesis": r.hypothesis,
                        "judged_correct": r.judged_correct,
                        "judge_raw": r.judge_raw,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                        "latency_ms": round(r.latency_ms, 1),
                    },
                )
                + "\n",
            )
        for qid in skipped_case_ids:
            fh.write(json.dumps({"question_id": qid, "skipped": True}) + "\n")


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
    dump_dir: Path | None = None,
) -> dict[str, EvalRunSummary]:
    """Run the full harness over ``instances``; return per-arm summaries.

    Forces ``GENESIS_MEMORY_WRITEBACKS_OFF`` for the duration of the run so
    multi-arm recall against a shared per-question store cannot re-rank memories
    across arms, and RESTORES the prior value afterwards so an in-process caller
    (e.g. the test suite) isn't polluted. Persists one run per arm when ``db``
    is provided. When ``dump_dir`` is set, writes one ``<arm>.jsonl``
    per-question diagnostics file per arm and records its path in the
    summary's metadata.
    """
    arms = list(arms) if arms is not None else default_arms()
    client = client or build_client()
    prior_writebacks = os.environ.get(_WRITEBACKS_OFF_ENV)
    os.environ[_WRITEBACKS_OFF_ENV] = "1"

    # No silent caps: if rerank arms are requested but no enabled reranker is
    # wired, those arms silently equal their non-rerank twins — say so loudly.
    wants_rerank = any(a.rerank for a in arms)
    reranker_enabled = bool(reranker) and bool(getattr(reranker, "enabled", False))
    if wants_rerank and not reranker_enabled:
        logger.warning(
            "rerank arms requested but no enabled reranker wired; those arms "
            "will produce identical results to their non-rerank twins",
        )

    # Build ONE embedder for the whole run (not per question): its httpx clients
    # + diskcache would otherwise accumulate ~2 per question and never close.
    # Sharing is safe (stateless per question) and reuses the connection pool +
    # cache across questions.
    owns_embedder = embedding_provider is None
    run_cache_dir: Path | None = None
    if owns_embedder:
        from genesis.memory.embeddings import EmbeddingProvider

        root = Path.home() / "tmp" / "longmemeval_runs"
        root.mkdir(parents=True, exist_ok=True)
        run_cache_dir = Path(tempfile.mkdtemp(prefix="lme_emb_", dir=str(root)))
        embedding_provider = EmbeddingProvider(
            backends=EmbeddingProvider.build_chain(ollama_first=False),
            cache_dir=run_cache_dir,
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

    try:
        gathered = await asyncio.gather(*[_one(i) for i in instances])
    finally:
        # Restore prior env so a direct in-process caller isn't polluted.
        if prior_writebacks is None:
            os.environ.pop(_WRITEBACKS_OFF_ENV, None)
        else:
            os.environ[_WRITEBACKS_OFF_ENV] = prior_writebacks
        if owns_embedder:
            await _close_embedder(embedding_provider)
            if run_cache_dir is not None:
                shutil.rmtree(run_cache_dir, ignore_errors=True)

    # Regroup by arm; a question with no results errored out (skipped) and is
    # recorded per-arm so the denominator reflects the requested N and provider
    # failures surface instead of silently shrinking the sample.
    by_arm: dict[str, list[QuestionArmResult]] = {a.label: [] for a in arms}
    skipped_qids: list[str] = []
    for inst, question_results in zip(instances, gathered, strict=True):
        if not question_results:
            skipped_qids.append(inst.question_id)
            continue
        for r in question_results:
            by_arm.setdefault(r.arm_label, []).append(r)

    summaries: dict[str, EvalRunSummary] = {}
    for arm in arms:
        arm_results = by_arm.get(arm.label, [])
        summary = build_run_summary(
            arm_label=arm.label,
            skipped_case_ids=skipped_qids,
            model=DEFAULT_MODEL,
            dataset=_DATASET,
            results=arm_results,
        )
        if dump_dir is not None:
            dump_path = Path(dump_dir) / f"{arm.label}.jsonl"
            write_dump_jsonl(dump_path, arm_results, skipped_case_ids=skipped_qids)
            summary.metadata["dump_path"] = str(dump_path)
        summaries[arm.label] = summary
        if db is not None:
            from genesis.eval.db import insert_run

            await insert_run(db, summary)
    return summaries


async def _close_embedder(embedder: object) -> None:
    """Best-effort close of an embedder's httpx clients + diskcache.

    ``EmbeddingProvider`` exposes no ``close()``; reach in defensively so a
    future internal change degrades to a no-op rather than raising.
    """
    for backend in getattr(embedder, "backends", []) or []:
        http_client = getattr(backend, "_client", None)
        if http_client is not None and hasattr(http_client, "aclose"):
            with contextlib.suppress(Exception):
                await http_client.aclose()
    cache = getattr(embedder, "_disk_cache", None)
    if cache is not None and hasattr(cache, "close"):
        with contextlib.suppress(Exception):
            cache.close()
