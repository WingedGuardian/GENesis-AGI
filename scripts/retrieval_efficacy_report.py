#!/usr/bin/env python3
"""Retrieval-efficacy report — pool-growth-vs-quality trend + LongMemEval arms.

The WS2 retrieval-drift work rests on a premise nobody had ever measured:
retrieval quality degraded as the memory pool grew ("69%->38%"). This report is
the first instrument that can substantiate or refute it. It joins the weekly
J-9 memory-quality snapshots (precision@k / hit_rate / MRR) to the pool size
they were measured over — using the ``pool_*`` keys stamped from WS2-0 onward,
and a read-only historical reconstruction (from ``memory_metadata.created_at``)
for pre-WS2-0 weeks. It also summarises the latest LongMemEval arm scores so a
lever's paired arm is visible next to the live trend, and renders each lever's
flip-gate checklist.

Read-only (mode=ro URI). Output: markdown to
``~/.genesis/output/retrieval_efficacy/`` (or ``--out``). Pure ``build_report``
is unit-tested; ``_load`` is the only DB-touching seam.

The historical pool reconstruction is an APPROXIMATION — it counts rows created
on or before each period end and cannot see dedup/forgetting deletions, so it
is biased high on older weeks. It is LABELLED as reconstructed in the output and
NEVER written back to ``eval_snapshots``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

_DATASET = "longmemeval_oracle"

# A lever does not flip live on this report alone — but the report renders the
# gate so the numbers are read against the agreed bar (Jay pulls each flip).
_GATE_CHECKLIST = (
    (
        "scope (PR-A)",
        "shadow wing-agreement >=80% over >=200 derived-wing recalls AND "
        "raw+scope arm evidence-coverage within noise of raw AND live fallback <30%",
    ),
    (
        "budget (PR-B)",
        "would-trim distribution sane (raise default rather than flip if >30% of "
        "recalls trim at 10K) AND raw+budget arm accuracy >= -1pp vs its twin",
    ),
    (
        "dedup (PR-C)",
        ">=2wk shadow verdict distribution AND manual audit of sampled duplicate "
        "verdicts >=95% precision AND raw+dedup arm >= -1pp with multi-session "
        "evidence-coverage inspected",
    ),
)


def build_report(
    *,
    snapshots: list[dict],
    reconstructed_pool: dict[str, int],
    lme_runs: list[dict],
) -> dict:
    """Assemble the report from already-loaded data (pure — no DB).

    ``snapshots``: weekly memory snapshots, newest-first, each a dict with
    ``period_end`` and a parsed ``metrics`` dict.
    ``reconstructed_pool``: ``{period_end: episodic_count}`` fallback pool sizes.
    ``lme_runs``: ``eval_runs`` rows (newest-first) for the LongMemEval dataset.
    """
    trend: list[dict] = []
    for snap in reversed(snapshots):  # DESC -> chronological
        metrics = snap.get("metrics", {}) or {}
        period_end = snap.get("period_end")
        pool = metrics.get("pool_episodic_total")
        pool_source = "snapshot"
        if pool is None:
            pool = reconstructed_pool.get(period_end)
            pool_source = "reconstructed" if pool is not None else "unknown"
        trend.append(
            {
                "period_end": period_end,
                "precision_at_5": metrics.get("precision_at_5"),
                "hit_rate": metrics.get("hit_rate"),
                "mrr": metrics.get("mrr"),
                "total_recalls": metrics.get("total_recalls"),
                "pool_episodic_total": pool,
                # Retrievable pool (deprecated/expired excluded) — the denominator
                # the drift hypothesis cares about. Only present from WS2-0 on;
                # reconstructed weeks have no retrievable count (current-state
                # flags can't be reconstructed historically) -> None.
                "pool_episodic_retrievable": metrics.get("pool_episodic_retrievable"),
                "pool_source": pool_source,
            }
        )

    # Drift check: earliest vs latest week that carries BOTH a quality number
    # and a pool size. Informational — a real trend needs several weeks.
    usable = [
        t for t in trend if t["hit_rate"] is not None and t["pool_episodic_total"] is not None
    ]
    drift: dict | None = None
    if len(usable) >= 2:
        first, last = usable[0], usable[-1]
        drift = {
            "first_period": first["period_end"],
            "last_period": last["period_end"],
            "first_hit_rate": first["hit_rate"],
            "last_hit_rate": last["hit_rate"],
            "first_pool": first["pool_episodic_total"],
            "last_pool": last["pool_episodic_total"],
            "pool_grew": last["pool_episodic_total"] > first["pool_episodic_total"],
            "quality_dropped": last["hit_rate"] < first["hit_rate"],
            "weeks_compared": len(usable),
        }

    # LongMemEval: the latest run per arm (model_profile "longmemeval:<label>").
    arms: dict[str, dict] = {}
    for run in lme_runs:  # newest-first
        profile = run.get("model_profile") or ""
        if profile.startswith("longmemeval:") and profile not in arms:
            arms[profile] = {
                "arm": profile.split("longmemeval:", 1)[1],
                "aggregate_score": run.get("aggregate_score"),
                "created_at": run.get("created_at"),
            }

    return {
        "trend": trend,
        "drift": drift,
        "longmemeval_arms": sorted(arms.values(), key=lambda a: a["arm"]),
        "n_snapshots": len(snapshots),
    }


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_md(report: dict, *, generated_at: str) -> str:
    lines: list[str] = [
        "# Retrieval-efficacy report",
        "",
        f"_Generated {generated_at} · {report['n_snapshots']} weekly memory snapshot(s)_",
        "",
        "## Pool-growth vs quality trend",
        "",
        "| Week ending | precision@5 | hit_rate | MRR | recalls | "
        "episodic pool | retrievable | pool source |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in report["trend"]:
        lines.append(
            f"| {_fmt(row['period_end'])} | {_fmt(row['precision_at_5'])} | "
            f"{_fmt(row['hit_rate'])} | {_fmt(row['mrr'])} | "
            f"{_fmt(row['total_recalls'])} | {_fmt(row['pool_episodic_total'])} | "
            f"{_fmt(row.get('pool_episodic_retrievable'))} | {row['pool_source']} |"
        )
    if not report["trend"]:
        lines.append("| _(no weekly memory snapshots yet)_ | | | | | | | |")

    lines += ["", "## Drift check", ""]
    drift = report["drift"]
    if drift is None:
        lines.append(
            "_Not enough usable weeks yet (need >=2 with both a quality number "
            "and a pool size). The drift premise stays UNTESTED until then._"
        )
    else:
        verdict = (
            "consistent with pool-growth drift"
            if drift["pool_grew"] and drift["quality_dropped"]
            else "no pool-growth drift in this window"
        )
        lines += [
            f"Comparing {drift['weeks_compared']} usable weeks "
            f"({drift['first_period']} -> {drift['last_period']}):",
            "",
            f"- episodic pool: {drift['first_pool']} -> {drift['last_pool']} "
            f"({'grew' if drift['pool_grew'] else 'did not grow'})",
            f"- hit_rate: {_fmt(drift['first_hit_rate'])} -> "
            f"{_fmt(drift['last_hit_rate'])} "
            f"({'dropped' if drift['quality_dropped'] else 'held/improved'})",
            f"- **Read: {verdict}.** Informational only — a few weeks is not a "
            "trend, and reconstructed pool sizes are biased high on older weeks.",
        ]

    lines += ["", "## LongMemEval arms (latest run per arm)", ""]
    if report["longmemeval_arms"]:
        lines += ["| Arm | aggregate score | run |", "|---|---|---|"]
        lines += [
            f"| {a['arm']} | {_fmt(a['aggregate_score'])} | {_fmt(a['created_at'])} |"
            for a in report["longmemeval_arms"]
        ]
    else:
        lines.append("_(no LongMemEval runs recorded yet)_")

    lines += ["", "## Lever flip-gate checklist", ""]
    lines += [f"- **{name}**: {crit}" for name, crit in _GATE_CHECKLIST]
    lines += [
        "",
        "_Each shadow->live flip is a per-lever decision made with this data in "
        "hand — the gate is rendered here, not auto-applied._",
        "",
    ]
    return "\n".join(lines)


async def _reconstruct_pool(db, period_ends: list[str]) -> dict[str, int]:
    """Episodic pool size as of each ``period_end`` from ``created_at``.

    APPROXIMATE — cannot see dedup/forgetting deletions, so biased high on older
    weeks. Never written back; only used to fill pre-WS2-0 snapshots. Delegates
    the count to the CRUD layer (``episodic_count_created_before``).
    """
    from genesis.db.crud import memory as memory_crud

    out: dict[str, int] = {}
    for pe in period_ends:
        if not pe:
            continue
        try:
            out[pe] = await memory_crud.episodic_count_created_before(db, pe)
        except Exception as exc:  # noqa: BLE001 - LOUD, never silently empty
            print(
                f"retrieval_efficacy_report: pool reconstruct failed at {pe}: {exc}",
                file=sys.stderr,
            )
    return out


async def _load(db_path: str) -> tuple[list[dict], dict[str, int], list[dict]]:
    """Read memory snapshots, reconstructed pool, and LongMemEval runs (RO)."""
    import aiosqlite

    from genesis.db.crud import j9_eval
    from genesis.eval import db as eval_db

    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as raw:
        raw.row_factory = aiosqlite.Row

        async def _safe(reader, label: str, **kwargs):
            try:
                return await reader(raw, **kwargs)
            except Exception as exc:  # noqa: BLE001 - LOUD; empty would mislead
                print(f"retrieval_efficacy_report: {label} read failed: {exc}", file=sys.stderr)
                return []

        snapshots = await _safe(
            j9_eval.get_snapshots,
            "memory snapshots",
            dimension="memory",
            period_type="weekly",
            limit=52,
        )
        lme_runs = await _safe(
            eval_db.get_runs,
            "longmemeval runs",
            dataset=_DATASET,
            limit=200,
        )
        period_ends = [s.get("period_end") for s in snapshots]
        reconstructed = await _reconstruct_pool(raw, period_ends)
        return snapshots, reconstructed, lme_runs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="genesis.db path (default: repo data dir)")
    ap.add_argument("--out", default=None, help="output md path (default: ~/.genesis/output/)")
    args = ap.parse_args()

    from genesis.env import genesis_db_path

    db_path = args.db or str(genesis_db_path())
    snapshots, reconstructed, lme_runs = asyncio.run(_load(db_path))
    report = build_report(
        snapshots=snapshots,
        reconstructed_pool=reconstructed,
        lme_runs=lme_runs,
    )
    now = datetime.now(UTC)
    md = render_md(report, generated_at=now.isoformat())
    out = Path(
        args.out
        or Path.home()
        / ".genesis"
        / "output"
        / "retrieval_efficacy"
        / f"retrieval_efficacy_{now.date()}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    drift = report["drift"]
    drift_note = (
        "untested (need >=2 usable weeks)"
        if drift is None
        else (
            "drift-consistent"
            if drift["pool_grew"] and drift["quality_dropped"]
            else "no-drift-in-window"
        )
    )
    print(
        f"snapshots={report['n_snapshots']} arms={len(report['longmemeval_arms'])} "
        f"drift={drift_note}"
    )
    print(f"report: {out}")


if __name__ == "__main__":
    main()
