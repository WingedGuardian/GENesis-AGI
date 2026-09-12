"""Recall-health golden-set probe — Phase 0 "make silence loud".

Runs an install-local golden set of ``query -> expected_memory_id(s)`` cases
through the REAL recall pipeline (``HybridRetriever.recall``) and measures
hit-rate + mean reciprocal rank, tracking drift against a trailing baseline.
This is the recall-quality half of the integrity spine: the consistency checker
proves a memory is *stored* correctly; the probe proves a memory is still
*findable*.

Golden set is an install-local FILE (``~/.genesis/eval/golden/
memory_integrity_recall.jsonl``) per the #1143 convention — real cases carry
recalled-memory ids (PII-adjacent) and never live in the repo; a committed
template documents the schema. Seed it with
``scripts/seed_recall_golden_set.py --suggest N``.

Probe guardrails (all real ``recall`` params):
- ``skip_writeback=lambda _r: True`` — recall() normally bumps activation on hits
  (read-MOSTLY). A daily probe that entrenched its golden memories would distort
  the very ranking it measures, so write-back is disabled for probe recalls.
- ``source="both"`` — the collection selector; the whole recall pipeline. NOTE:
  probe recalls still emit a ``recall_fired`` eval event (skip_writeback does not
  gate the emit, and ``source`` is not a free attribution tag), so ~one event
  per golden case per run lands in ``eval_events`` tagged ``both`` — a small,
  monitor-only footprint. The seeder therefore excludes queries ALREADY in the
  golden set (not by source) so the set can never feed on itself.
- ``rerank_timeout_s`` bounds the cross-encoder stage; ``max_probes`` caps work.

Fail-closed: an empty/too-small golden set → ``unknown`` (needs setup, not
alarmed); a retriever that is absent or raises → ``unknown`` (a measurement
failure must not read as recall degradation).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_GOLDEN = Path.home() / ".genesis" / "eval" / "golden" / "memory_integrity_recall.jsonl"


@dataclass
class RecallProbeResult:
    status: str  # healthy | degraded | unknown
    probes_total: int = 0
    probes_hit: int = 0
    hit_rate: float | None = None
    mean_rr: float | None = None
    baseline_hit_rate: float | None = None
    drift: float | None = None
    details: list[dict] = field(default_factory=list)
    unknown_reason: str | None = None
    duration_ms: int = 0


def load_golden_set(path: Path | str | None = None) -> list[dict]:
    """Load golden cases from the install-local JSONL file.

    Skips blank lines and ``#`` comments (the template convention). Each case
    must carry a ``query`` and a non-empty ``expected_memory_ids`` list; malformed
    lines are skipped with a warning rather than crashing the probe. Returns
    ``[]`` when the file is absent (fresh install) — the caller maps that to
    ``unknown``.
    """
    p = Path(path) if path else _DEFAULT_GOLDEN
    if not p.exists():
        return []
    cases: list[dict] = []
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("recall_probe: skipping malformed golden line", exc_info=True)
            continue
        query = case.get("query")
        expected = case.get("expected_memory_ids") or []
        if not query or not isinstance(expected, list) or not expected:
            logger.warning("recall_probe: skipping golden case missing query/expected_memory_ids")
            continue
        cases.append(
            {"id": case.get("id", ""), "query": query, "expected": [str(e) for e in expected]}
        )
    return cases


async def run_recall_probe(
    *,
    db,
    retriever,
    golden_path: Path | str | None = None,
    probe_limit: int = 10,
    max_probes: int = 25,
    rerank: bool = True,
    rerank_timeout_s: float = 10.0,
    min_golden_for_status: int = 5,
    baseline_window_runs: int = 7,
    baseline_min_runs: int = 3,
    drift_band: float = 0.2,
) -> RecallProbeResult:
    """Run the recall-health probe. ``db`` is used only to read the trailing
    baseline (persistence is the caller's job)."""
    start = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - start) * 1000)

    if retriever is None:
        return RecallProbeResult(
            status="unknown", unknown_reason="retriever_unavailable", duration_ms=_elapsed()
        )

    golden = load_golden_set(golden_path)
    if len(golden) < min_golden_for_status:
        return RecallProbeResult(
            status="unknown",
            probes_total=len(golden),
            unknown_reason="golden_set_too_small",
            duration_ms=_elapsed(),
        )

    cases = golden[:max_probes]
    details: list[dict] = []
    hits = 0
    rr_sum = 0.0
    for case in cases:
        try:
            results = await retriever.recall(
                case["query"],
                limit=probe_limit,
                rerank=rerank,
                rerank_timeout_s=rerank_timeout_s,
                # `source` is the COLLECTION selector (episodic|knowledge|both),
                # NOT an attribution tag — an invalid value raises ValueError.
                # 'both' exercises the whole recall pipeline the probe measures.
                source="both",
                # skip activation write-back so a daily probe never entrenches
                # its own golden memories and distorts the ranking it measures.
                skip_writeback=lambda _r: True,
            )
        except Exception as exc:
            # A measurement failure (provider/rerank/DB outage) must NOT be
            # scored as a recall miss — that would fabricate degradation. Abort
            # the whole run to 'unknown', consistent with the checker's stance.
            logger.warning("recall_probe: recall() failed — reporting unknown", exc_info=True)
            return RecallProbeResult(
                status="unknown",
                probes_total=len(cases),
                unknown_reason=f"recall_error: {type(exc).__name__}",
                duration_ms=_elapsed(),
            )
        retrieved = [str(r.memory_id) for r in results]
        expected = set(case["expected"])
        rank: int | None = None
        for i, mid in enumerate(retrieved):
            if mid in expected:
                rank = i + 1
                break
        hit = rank is not None
        if hit:
            hits += 1
            rr_sum += 1.0 / rank
        details.append({"id": case["id"], "hit": hit, "rank": rank})

    total = len(cases)
    hit_rate = hits / total if total else 0.0
    mean_rr = rr_sum / total if total else 0.0

    # Trailing baseline over PRIOR non-unknown runs (current run not yet stored).
    from genesis.db.crud import memory_integrity as mi_crud

    baseline, n_runs = await mi_crud.trailing_hit_rate(db, window=baseline_window_runs)
    if baseline is None or n_runs < baseline_min_runs:
        # Observation period — not enough history to call drift yet.
        status = "healthy"
        baseline = None
        drift = None
    else:
        drift = baseline - hit_rate
        status = "degraded" if drift > drift_band else "healthy"

    return RecallProbeResult(
        status=status,
        probes_total=total,
        probes_hit=hits,
        hit_rate=hit_rate,
        mean_rr=mean_rr,
        baseline_hit_rate=baseline,
        drift=drift,
        details=details,
        duration_ms=_elapsed(),
    )
