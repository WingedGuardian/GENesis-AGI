"""Calibration-cell aggregation (WS-2 P3) — graded rows → the unified table.

Runs at the end of each mechanical grading pass (``grader.py`` calls
``recompute_calibration_cells``; failures are counted, never allowed to break
grading). Pure math + orchestration only: imports stay stdlib +
``genesis.db.crud`` + ``genesis.calibration.metrics`` so the grader's
no-LLM-import lock (``test_no_llm_import_path``) holds through this module.

Lanes: ``stated`` cells aggregate ONLY provenance='stated' rows and
``policy_prior`` cells only prior rows — the lanes are partitioned at grouping
time and can never contaminate each other (a prior betting the base rate must
not launder itself into "self-knowledge"; design §4.2). The ``all`` lane is a
coverage/ops view over the union.

Murphy decomposition per cell over fixed confidence deciles
(bin k = ``min(int(c * 10), 9)``):

    brier       = (1/N) Σᵢ (cᵢ − oᵢ)²          (true per-row mean)
    reliability = (1/N) Σₖ n_k (c̄_k − ō_k)²
    resolution  = (1/N) Σₖ n_k (ō_k − ō)²
    uncertainty = ō (1 − ō)

The identity ``brier = reliability − resolution + uncertainty`` holds exactly
when every confidence within a bin is equal; with mixed within-bin
confidences it holds up to two within-bin variance/covariance terms
(Stephenson et al. 2008, "Two extra components in the Brier score
decomposition"; Siegert 2017, QJRMS). We store the true per-row ``brier``
plus the three binned terms — consumers must not assume the identity is
exact. ECE reuses the pure ``compute_ece`` over the SAME bins.

Hierarchical shrinkage (design §3.4): Beta-binomial with prior strength
``m = 10`` — cell → parent domain (dotted prefix) → global, computed within a
(action_class, metric, lane, window) stratum. Both raw ``base_rate`` and
``shrunk_estimate`` are stored; consumers read the shrunk value but display
``n``.

Tool-call lane (design §4.4, strict): per-tool success base rates from
``tool_call_outcomes`` land as ``provenance='policy_prior'``,
``action_class='tool_call'``, ``metric='success_rate'``, domain
``tool.<tool_name>`` — n and base_rate ONLY (no brier family, no shrinkage:
nothing was predicted).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from genesis.calibration.metrics import compute_ece
from genesis.db.crud import calibration_cells as cells_crud
from genesis.db.crud import ledger_predictions as lp_crud
from genesis.db.crud import tool_call_outcomes as tco_crud
from genesis.db.timeutil import canonical_iso

logger = logging.getLogger(__name__)

PRIOR_STRENGTH: float = 10.0  # Beta-binomial m (design §3.4)
WINDOWS: tuple[int, ...] = (30, 90, 0)  # days; 0 = all-time
HISTORY_RETENTION_DAYS: int = 180
_N_BINS: int = 10  # confidence deciles shared by Murphy terms and ECE

# Cold-start honesty thresholds (design §3.4).
_OK_N: int = 30
_THIN_N: int = 10

# Process-global failure counters, mirroring the grader's pattern
# (grader.py `_grade_failed`). Read by observability; reset per test.
_recompute_failed: Counter[str] = Counter()


def cell_recompute_failure_counts() -> dict[str, int]:
    """Snapshot of recompute failures since process start (observability)."""
    return dict(_recompute_failed)


def record_recompute_failure() -> None:
    """Bump the failure counter — the ONE write path (the grader's except arm)."""
    _recompute_failed["recompute"] += 1


def _reset_cell_counters_for_tests() -> None:
    _recompute_failed.clear()


@dataclass
class CellReport:
    """What one recompute pass did."""

    cells_written: int = 0
    history_appended: int = 0
    history_pruned: int = 0


# ── pure, hand-testable math ──────────────────────────────────────────────────


def parent_domain(domain: str) -> str | None:
    """``'outreach.general'`` → ``'outreach'``; dotless → None (parent = global)."""
    head, sep, _ = domain.partition(".")
    return head if sep else None


def status_for(n: int) -> str:
    """Cold-start honesty label: ok (n≥30) / thin (10≤n<30) / unknown (n<10)."""
    if n >= _OK_N:
        return "ok"
    if n >= _THIN_N:
        return "thin"
    return "unknown"


def shrink(successes: int, n: int, prior_rate: float, m: float = PRIOR_STRENGTH) -> float:
    """Beta-binomial posterior mean: ``(successes + m·prior) / (n + m)``."""
    return (successes + m * prior_rate) / (n + m)


def compute_cell_stats(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    """Murphy decomposition + ECE for one cell's (confidence, outcome) pairs."""
    n = len(pairs)
    if n == 0:
        return {
            "n": 0,
            "base_rate": None,
            "mean_confidence": None,
            "brier": None,
            "reliability": None,
            "resolution": None,
            "uncertainty": None,
            "ece": None,
        }
    overall = sum(o for _, o in pairs) / n
    brier = sum((c - o) ** 2 for c, o in pairs) / n

    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for c, o in pairs:
        bins[min(int(c * _N_BINS), _N_BINS - 1)].append((c, o))

    reliability = 0.0
    resolution = 0.0
    curves: list[dict[str, float]] = []
    for members in bins.values():
        n_k = len(members)
        conf_k = sum(c for c, _ in members) / n_k
        rate_k = sum(o for _, o in members) / n_k
        reliability += n_k * (conf_k - rate_k) ** 2
        resolution += n_k * (rate_k - overall) ** 2
        curves.append(
            {
                "sample_count": n_k,
                "predicted_confidence": conf_k,
                "actual_success_rate": rate_k,
            }
        )
    return {
        "n": n,
        "base_rate": overall,
        "mean_confidence": sum(c for c, _ in pairs) / n,
        "brier": brier,
        "reliability": reliability / n,
        "resolution": resolution / n,
        "uncertainty": overall * (1.0 - overall),
        "ece": compute_ece(curves),
    }


# ── orchestration ─────────────────────────────────────────────────────────────


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _build_prediction_cells(rows: list[dict], *, now: datetime) -> list[dict]:
    """All prediction-lane cells across every window."""
    cells: list[dict] = []
    for window in WINDOWS:
        cutoff = None if window == 0 else now - timedelta(days=window)
        in_window = [
            r
            for r in rows
            if cutoff is None or ((ts := _parse_ts(r["resolved_at"])) is not None and ts >= cutoff)
        ]
        # Lane partition at grouping time — stated never sees prior rows.
        lanes: dict[str, list[dict]] = {
            "stated": [r for r in in_window if r["provenance"] == "stated"],
            "policy_prior": [r for r in in_window if r["provenance"] == "policy_prior"],
            "all": in_window,
        }
        for lane, lane_rows in lanes.items():
            groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
            for r in lane_rows:
                groups[(r["domain"], r["action_class"], r["metric"])].append(r)
            if not groups:
                continue

            # Shrinkage pools within the (action_class, metric, lane, window)
            # stratum: global rate + per-parent-domain rates.
            strata: dict[tuple[str, str], list[int]] = defaultdict(list)
            parents: dict[tuple[str, str, str], list[int]] = defaultdict(list)
            for (domain, action_class, metric), members in groups.items():
                outcomes = [r["outcome_value"] for r in members]
                strata[(action_class, metric)].extend(outcomes)
                head = parent_domain(domain) or domain
                parents[(action_class, metric, head)].extend(outcomes)

            for (domain, action_class, metric), members in sorted(groups.items()):
                pairs = [(r["confidence"], r["outcome_value"]) for r in members]
                stats = compute_cell_stats(pairs)
                successes = sum(o for _, o in pairs)
                pool = strata[(action_class, metric)]
                global_rate = sum(pool) / len(pool)
                head = parent_domain(domain)
                if head is None:
                    prior = global_rate
                else:
                    parent_pool = parents[(action_class, metric, head)]
                    prior = shrink(sum(parent_pool), len(parent_pool), global_rate)
                cells.append(
                    {
                        "domain": domain,
                        "action_class": action_class,
                        "metric": metric,
                        "provenance": lane,
                        "window_days": window,
                        "n_mechanical": sum(
                            1
                            for r in members
                            if r["resolver"] in ("mechanical", "mechanical_absence")
                        ),
                        "shrunk_estimate": shrink(successes, stats["n"], prior),
                        "status": status_for(stats["n"]),
                        **stats,
                    }
                )
    return cells


async def _build_tool_cells(db: Any, *, now: datetime) -> list[dict]:
    """Per-tool success base rates (design §4.4, strict: n + base_rate only)."""
    cells: list[dict] = []
    for window in WINDOWS:
        since = None if window == 0 else canonical_iso((now - timedelta(days=window)).isoformat())
        rows = await tco_crud.aggregate_success_rates(db, since=since)
        for row in rows:
            n = int(row["n"])
            cells.append(
                {
                    "domain": f"tool.{row['tool_name']}",
                    "action_class": "tool_call",
                    "metric": "success_rate",
                    "provenance": "policy_prior",
                    "window_days": window,
                    "n": n,
                    "n_mechanical": n,  # every row is a mechanical observation
                    "base_rate": row["base_rate"],
                    # Nothing was predicted: no confidence, no Brier family,
                    # and (strict §4.4) no shrunk estimate either.
                    "mean_confidence": None,
                    "brier": None,
                    "reliability": None,
                    "resolution": None,
                    "uncertainty": None,
                    "ece": None,
                    "shrunk_estimate": None,
                    "status": status_for(n),
                }
            )
    return cells


async def recompute_calibration_cells(db: Any, *, now: datetime | None = None) -> CellReport:
    """Full recompute of ``calibration_cells`` + one history snapshot + prune.

    May raise — the grader wraps this call and counts failures; grading is
    never blocked by a cell-recompute problem (derived data goes stale, the
    counter and the error log say so).
    """
    now = now or datetime.now(UTC)
    rows = await lp_crud.list_resolved(db)

    cells = _build_prediction_cells(rows, now=now)
    cells.extend(await _build_tool_cells(db, now=now))

    written = await cells_crud.replace_cells(db, cells, now=now)
    appended = await cells_crud.append_history(db, cells, now=now)
    retention_floor = canonical_iso((now - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat())
    pruned = await cells_crud.prune_history(db, before=retention_floor)
    report = CellReport(cells_written=written, history_appended=appended, history_pruned=pruned)
    logger.info(
        "calibration cells recomputed: %d cells, %d snapshots, %d pruned",
        written,
        appended,
        pruned,
    )
    return report
