"""Orchestration + aggregation for the LongMemEval harness.

Per question: baseline arms (query-mode x rerank) share ONE fresh ephemeral
store ingested link-free; graph arms get a SECOND store ingested with
``auto_link=True`` plus 1-hop link expansion at recall time (links shift
ranking for every arm sharing a store — graph boost AND activation
connectivity — so baselines never see a linked store). Recall write-backs are
suppressed via ``GENESIS_MEMORY_WRITEBACKS_OFF`` so arms don't contaminate
each other. Each arm's recalled memories feed the reader; the hypothesis is
graded by the gpt-4o judge. Results aggregate per-arm into an
``EvalRunSummary`` (one persisted run per arm) with overall +
per-question-type accuracy. A store-block failure skips ONLY that block's
arms — completed (paid, judged) results from the other block are kept, so the
skip ledger never records an attempted arm as "NOT attempted".
"""

from __future__ import annotations

import asyncio
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
from genesis.memory.linker import DEFAULT_SIMILARITY_THRESHOLD

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import aiosqlite

    from genesis.eval.longmemeval.dataset import LongMemEvalInstance
    from genesis.eval.longmemeval.ingest import IngestResult
    from genesis.eval.longmemeval.store import EphemeralStore

logger = logging.getLogger("genesis.eval.longmemeval")

# gpt-4o-2024-08-06 pricing (USD per token).
_COST_IN = 2.50 / 1_000_000
_COST_OUT = 10.0 / 1_000_000

_DATASET = "longmemeval_oracle"
_WRITEBACKS_OFF_ENV = "GENESIS_MEMORY_WRITEBACKS_OFF"


@dataclass(frozen=True)
class Arm:
    """One evaluation arm: a query mode x rerank on/off x graph on/off.

    ``graph`` arms run against a SEPARATE ephemeral store ingested with
    ``auto_link=True`` (see ``run_question``) and add 1-hop link expansion
    after top-K recall.
    """

    query_arm: QueryArm
    rerank: bool
    graph: bool = False
    #: A registered retrieval-config variant (WS2-0); "" = baseline. Each WS2
    #: lever PR registers one in VARIANTS and adds its paired arm here.
    variant: str = ""

    @property
    def label(self) -> str:
        # Deterministic suffix order (rerank, then graph, then variant):
        # model_profile "longmemeval:<label>" and dump filenames must be stable
        # across runs, and every pre-WS2-0 label stays byte-identical because
        # the +variant suffix only appears when a variant is set.
        suffix = (
            ("+rerank" if self.rerank else "")
            + ("+graph" if self.graph else "")
            + (f"+{self.variant}" if self.variant else "")
        )
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
class ArmVariant:
    """A registered retrieval-config variant an eval arm can carry (WS2-0).

    The instrumentation PR ships the machinery with an EMPTY registry; each WS2
    lever PR registers exactly one variant here and adds its paired arm, so the
    levers stay additive instead of re-opening this file's core arm logic. Two
    application seams cover the recall-side levers:

    * ``recall_kwargs(instance, arm) -> dict`` — extra kwargs merged into the
      per-question ``recall()`` call (e.g. scope routing derives ``wing`` from
      the question text).
    * ``post_recall(memories) -> memories`` — transforms the final content list
      fed to the reader (e.g. an output token budget trims the relevance-sorted
      tail). Evidence metrics are computed from the untrimmed recall, so a
      budget arm stays coverage-comparable while its effect shows in the answer.

    ``recall_kwarg_names`` declares the ``recall()`` kwargs the variant injects;
    it is validated against the live ``recall()`` signature BEFORE any paid run
    (fail-fast, mirroring ``filter_arms``' label check).
    """

    name: str
    recall_kwarg_names: tuple[str, ...] = ()
    recall_kwargs: Callable[[LongMemEvalInstance, Arm], dict] | None = None
    post_recall: Callable[[list[str]], list[str]] | None = None


#: Registered variants, keyed by name. Empty until a lever PR registers one.
VARIANTS: dict[str, ArmVariant] = {}


def register_variant(variant: ArmVariant) -> None:
    """Register a retrieval-config variant (idempotent by name; last wins)."""
    VARIANTS[variant.name] = variant


def _recall_param_names() -> set[str]:
    """The keyword parameters ``HybridRetriever.recall`` accepts."""
    import inspect

    from genesis.memory.retrieval import HybridRetriever

    return set(inspect.signature(HybridRetriever.recall).parameters)


def validate_variants(arms: list[Arm]) -> None:
    """Fail-fast before any spend: every arm's variant must be registered and
    its declared recall kwargs must exist on ``recall()``. Raises ValueError."""
    recall_params = _recall_param_names()
    for arm in arms:
        if not arm.variant:
            continue
        variant = VARIANTS.get(arm.variant)
        if variant is None:
            msg = f"unknown arm variant {arm.variant!r}; registered: {sorted(VARIANTS)}"
            raise ValueError(msg)
        unknown = sorted(set(variant.recall_kwarg_names) - recall_params)
        if unknown:
            msg = f"variant {arm.variant!r} injects unknown recall kwargs {unknown}"
            raise ValueError(msg)


def select_arms(
    *,
    no_rerank: bool = False,
    graph: bool = False,
    variants: Sequence[str] = (),
) -> list[Arm]:
    """Build the CLI's arm list: default arms, optionally rerank-filtered,
    optionally doubled with a ``+graph`` variant of every selected arm, then
    doubled again with each named ``+variant`` twin (paired baseline-vs-lever
    comparison in one run). Variant twins are only made from non-variant arms,
    so ``--variants`` never produces variant-of-variant labels."""
    from dataclasses import replace

    arms = default_arms()
    if no_rerank:
        arms = [a for a in arms if not a.rerank]
    if graph:
        arms = [*arms, *(replace(a, graph=True) for a in arms)]
    for name in variants:
        arms = [*arms, *(replace(a, variant=name) for a in arms if not a.variant)]
    return arms


def filter_arms(arms: list[Arm], only: str) -> list[Arm]:
    """Keep only the arms whose label appears in the CSV ``only``.

    Filters the already-selected universe (``select_arms`` output), preserving
    its order. Raises ``ValueError`` on labels outside that universe (e.g. a
    ``+graph`` label without ``--graph``) and on an empty selection — a paid
    run must never silently fall back to more arms than asked for.
    """
    wanted = {label.strip() for label in only.split(",") if label.strip()}
    if not wanted:
        msg = "no arms selected: --arms was empty"
        raise ValueError(msg)
    available = [a.label for a in arms]
    unknown = sorted(wanted - set(available))
    if unknown:
        msg = f"unknown arm labels {unknown}; selectable here: {available}"
        raise ValueError(msg)
    return [a for a in arms if a.label in wanted]


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
    #: Graph-arm diagnostics: links formed at ingest on this question's linked
    #: store; neighbor ids merged by 1-hop expansion; evidence metrics over the
    #: EXPANDED set (top-K ∪ expanded). All None/empty/0 on baseline arms —
    #: top-K metrics above stay baseline-comparable.
    links_created: int = 0
    expanded_ids: tuple[str, ...] = ()
    evidence_recalled_final: bool | None = None
    evidence_coverage_final: float | None = None
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
    graph_arm: bool | None = None,
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
    # Graph-arm means: post-expansion coverage + link/expansion volume — how
    # much the graph ADDED beyond top-K (ranking lift vs coverage lift split).
    finals = [r.evidence_coverage_final for r in results if r.evidence_coverage_final is not None]
    if finals:
        scores["evidence_coverage_final_mean"] = round(fmean(finals), 4)
    # Explicit flag from the caller (which has the Arm); label-suffix fallback
    # only for direct build_run_summary callers that predate the flag.
    if graph_arm is None:
        graph_arm = "+graph" in arm_label
    graph_metadata: dict[str, float] = {}
    if results and graph_arm:
        graph_metadata["links_created_mean"] = round(fmean(r.links_created for r in results), 2)
        graph_metadata["expanded_mean"] = round(fmean(len(r.expanded_ids) for r in results), 2)

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
        metadata={"arm": arm_label, "dataset": dataset, "skipped": skipped, **graph_metadata},
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
    link_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[QuestionArmResult]:
    """Run every arm for one question.

    Baseline and graph arms use SEPARATE ephemeral stores: links shift ranking
    for every arm sharing a store (graph boost AND activation connectivity,
    ``activation.py`` link_count term), so a shared linked store would tint the
    baselines and break comparability. The second ingest re-embeds through the
    run-level embedding cache, so its marginal cost is in-memory upserts + one
    local Qdrant search per turn.
    """
    out: list[QuestionArmResult] = []
    base_arms = [a for a in arms if not a.graph]
    graph_arms = [a for a in arms if a.graph]
    base_embedded: int | None = None

    # Each store block fails INDEPENDENTLY: a graph-store failure must not
    # discard already-completed (paid, judged) baseline results — that would
    # record attempted arms as "NOT attempted" skips and make the same
    # baseline arm label carry a different N in --graph vs plain runs.
    if base_arms:
        try:
            async with ephemeral_store(
                embedding_provider=embedding_provider,
                reranker=reranker,
            ) as es:
                ingest = await ingest_haystack(es.store, instance)
                base_embedded = await _embedded_count(es.db)
                for arm in base_arms:
                    out.append(await _run_arm(es, ingest, instance, arm, k=k, client=client))
        except Exception:
            logger.exception(
                "question %s: baseline store block failed; skipping baseline arms",
                instance.question_id,
            )

    if graph_arms:
        try:
            async with ephemeral_store(
                embedding_provider=embedding_provider,
                reranker=reranker,
                with_linker=True,
                link_threshold=link_threshold,
            ) as es:
                ingest = await ingest_haystack(es.store, instance, auto_link=True)
                rows = await es.db.execute_fetchall("SELECT COUNT(*) FROM memory_links")
                links_created = rows[0][0]
                if links_created == 0:
                    # No-silent-caps parity with the rerank warning: a link-free
                    # store makes graph arms equal their baseline twins.
                    logger.warning(
                        "graph arm: store for %s formed zero links (threshold %.2f); "
                        "graph arms will match their baseline twins on this question",
                        instance.question_id,
                        link_threshold,
                    )
                # Corpus-parity guard: a transient embedding failure routes a
                # turn to pending_embeddings (FTS5-only) SILENTLY — if the two
                # stores diverge, a graph-vs-baseline delta on this question
                # measures embedding weather, not graph value. Warn loudly.
                graph_embedded = await _embedded_count(es.db)
                if base_embedded is not None and graph_embedded != base_embedded:
                    logger.warning(
                        "question %s: store corpora diverged (baseline %d vs graph %d "
                        "embedded memories) — graph-vs-baseline delta unreliable here",
                        instance.question_id,
                        base_embedded,
                        graph_embedded,
                    )
                for arm in graph_arms:
                    out.append(
                        await _run_arm(
                            es,
                            ingest,
                            instance,
                            arm,
                            k=k,
                            client=client,
                            links_created=links_created,
                        ),
                    )
        except Exception:
            logger.exception(
                "question %s: graph store block failed; skipping graph arms "
                "(baseline results kept)",
                instance.question_id,
            )
    return out


async def _embedded_count(db: object) -> int:
    """Count fully-embedded memories in an ephemeral store (parity check)."""
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM memory_metadata WHERE embedding_status = 'embedded'",
    )
    return rows[0][0]


def _coverage(evidence_ids: set[str], ids: set[str]) -> tuple[bool, float | None]:
    """(any-hit, |evidence ∩ ids| / |evidence|); coverage None when the
    question has no evidence turns (abstention) — excluded from means."""
    hit = bool(evidence_ids & ids)
    cov = len(evidence_ids & ids) / len(evidence_ids) if evidence_ids else None
    return hit, cov


async def _expand_neighbors(db: object, seed_ids: list[str], *, k: int) -> list[tuple[str, str]]:
    """1-hop expansion: (memory_id, content) for up to ``k`` linked neighbors.

    Thin adapter over the PRODUCTION primitive
    ``genesis.memory.graph_expansion.expand_neighbors`` — the benchmark must
    measure the shipped recall-expansion code, not a harness re-implementation.
    The primitive is mode-independent (reads NO prod config; this harness
    passes explicit args only): dangling links skipped, dedup +
    MAX(strength) ordering, seeds never returned. No ``exclude_link_types``
    passed — eval ingest only ever stores supports/extends edges, so prod's
    configured ``contradicts`` exclusion is a no-op here by construction.
    """
    from genesis.memory.graph_expansion import expand_neighbors

    expanded = await expand_neighbors(db, seed_ids, cap=k)
    return [(r.memory_id, r.content) for r in expanded]


async def _run_arm(
    es: EphemeralStore,
    ingest: IngestResult,
    instance: LongMemEvalInstance,
    arm: Arm,
    *,
    k: int,
    client: object,
    links_created: int = 0,
) -> QuestionArmResult:
    """Recall (+ optional 1-hop graph expansion) -> answer -> judge, one arm.

    ``links_created`` is a STORE-WIDE ingest count for this question's linked
    store — it is stamped identically on every graph arm of the question (an
    ingest metric, not a per-arm measurement).
    """
    t0 = time.monotonic()
    query = build_query(instance.question, arm.query_arm)
    variant = VARIANTS.get(arm.variant) if arm.variant else None
    recall_extra = variant.recall_kwargs(instance, arm) if variant and variant.recall_kwargs else {}
    hits = await es.retriever.recall(
        query,
        source="episodic",
        limit=k,
        rerank=arm.rerank,
        **recall_extra,
    )
    recalled_ids = {h.memory_id for h in hits}
    evidence_ids = ingest.evidence_memory_ids
    memories = [h.content for h in hits]

    expanded_ids: tuple[str, ...] = ()
    evidence_recalled_final: bool | None = None
    evidence_coverage_final: float | None = None
    if arm.graph:
        expanded = await _expand_neighbors(es.db, [h.memory_id for h in hits], k=k)
        expanded_ids = tuple(mid for mid, _ in expanded)
        memories = [*memories, *(content for _, content in expanded)]
        evidence_recalled_final, evidence_coverage_final = _coverage(
            evidence_ids,
            recalled_ids | set(expanded_ids),
        )

    evidence_recalled, evidence_coverage = _coverage(evidence_ids, recalled_ids)
    # Variant post-recall transform (e.g. output token budget) acts on the
    # final content list fed to the reader — AFTER graph expansion and AFTER the
    # evidence metrics above, so coverage stays comparable to baseline while the
    # transform's effect surfaces only in the answer.
    if variant and variant.post_recall:
        memories = variant.post_recall(memories)
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
    return QuestionArmResult(
        question_id=instance.question_id,
        question_type=instance.question_type,
        arm_label=arm.label,
        hypothesis=ans.hypothesis,
        judged_correct=verdict.label,
        evidence_recalled=evidence_recalled,
        evidence_coverage=evidence_coverage,
        query=query,
        recalled_ids=tuple(h.memory_id for h in hits),
        links_created=links_created if arm.graph else 0,
        expanded_ids=expanded_ids,
        evidence_recalled_final=evidence_recalled_final,
        evidence_coverage_final=evidence_coverage_final,
        judge_raw=verdict.raw,
        input_tokens=ans.input_tokens + verdict.input_tokens,
        output_tokens=ans.output_tokens + verdict.output_tokens,
        latency_ms=(time.monotonic() - t0) * 1000,
    )


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
                        "links_created": r.links_created,
                        "expanded_ids": list(r.expanded_ids),
                        "evidence_recalled_final": r.evidence_recalled_final,
                        "evidence_coverage_final": r.evidence_coverage_final,
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
    link_threshold: float | None = None,
) -> dict[str, EvalRunSummary]:
    """Run the full harness over ``instances``; return per-arm summaries.

    Forces ``GENESIS_MEMORY_WRITEBACKS_OFF`` for the duration of the run so
    multi-arm recall against the shared per-question stores (baseline and, in
    graph mode, a second linked store) cannot re-rank memories across arms,
    and RESTORES the prior value afterwards so an in-process caller (e.g. the
    test suite) isn't polluted. Persists one run per arm when ``db`` is
    provided. When ``dump_dir`` is set, writes one ``<arm>.jsonl``
    per-question diagnostics file per arm and records its path in the
    summary's metadata. Skips are attributed PER ARM: a store-block failure
    inside ``run_question`` skips only that block's arms.
    """

    if link_threshold is None:
        link_threshold = DEFAULT_SIMILARITY_THRESHOLD
    arms = list(arms) if arms is not None else default_arms()
    labels = [a.label for a in arms]
    if len(labels) != len(set(labels)):
        # Duplicate labels would double-persist identical eval_runs rows.
        msg = f"duplicate arm labels: {sorted(labels)}"
        raise ValueError(msg)
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
                    link_threshold=link_threshold,
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
            from genesis.eval.longmemeval.store import close_embedder

            await close_embedder(embedding_provider)
            if run_cache_dir is not None:
                shutil.rmtree(run_cache_dir, ignore_errors=True)

    # Regroup by arm with PER-ARM skip attribution: a question is skipped for
    # an arm iff that arm produced no result (a store-block failure inside
    # run_question skips only that block's arms; completed arms keep their
    # results). Denominators reflect the requested N per arm and provider
    # failures surface instead of silently shrinking the sample.
    by_arm: dict[str, list[QuestionArmResult]] = {a.label: [] for a in arms}
    for question_results in gathered:
        for r in question_results:
            by_arm.setdefault(r.arm_label, []).append(r)

    summaries: dict[str, EvalRunSummary] = {}
    for arm in arms:
        arm_results = by_arm.get(arm.label, [])
        answered = {r.question_id for r in arm_results}
        arm_skipped = [i.question_id for i in instances if i.question_id not in answered]
        summary = build_run_summary(
            arm_label=arm.label,
            skipped_case_ids=arm_skipped,
            model=DEFAULT_MODEL,
            dataset=_DATASET,
            results=arm_results,
            graph_arm=arm.graph,
        )
        if dump_dir is not None:
            dump_path = Path(dump_dir) / f"{arm.label}.jsonl"
            write_dump_jsonl(dump_path, arm_results, skipped_case_ids=arm_skipped)
            summary.metadata["dump_path"] = str(dump_path)
        summaries[arm.label] = summary
        if db is not None:
            from genesis.eval.db import insert_run

            await insert_run(db, summary)
    return summaries
