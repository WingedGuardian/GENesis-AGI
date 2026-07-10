"""Bench report — console rendering + JSON persistence.

Honesty contract: every surface carries ``judge_calibrated: false`` (until a
human-labeled golden set calibrates the rubric), the rubric version (the
series-break marker), the task-file sha256 (ex-ante criteria freeze), and the
pilot-N caveat when McNemar returns insufficient_data.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

from genesis.eval.bench.types import BenchReport

logger = logging.getLogger(__name__)


def report_to_dict(report: BenchReport) -> dict:
    """JSON-able dict of the full report (task prompts/criteria INCLUDED —
    the JSON lives outside the repo, next to the private task set)."""
    return dataclasses.asdict(report)


def write_report(report: BenchReport, run_dir: Path, output_dir: Path) -> Path:
    """Write the JSON report into the run dir AND ~/.genesis/output/.

    The output-dir copy survives run-dir cleanup and is the replay source if
    DB persistence failed. Returns the output-dir path.
    """
    payload = json.dumps(report_to_dict(report), indent=2, default=str)
    (run_dir / "bench_report.json").write_text(payload, encoding="utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"bench_report_{report.run_id}.json"
    out.write_text(payload, encoding="utf-8")
    return out


def _fmt_stat(stats: dict) -> str:
    if not stats:
        return "  (no stats — no complete pairs)"
    lines = []
    if "control_mean_score" in stats:
        lines.append(
            f"  mean judge score: bare {stats['control_mean_score']:.3f} → "
            f"genesis {stats['treatment_mean_score']:.3f} "
            f"(Δ {stats['mean_delta']:+.3f})"
        )
    else:
        lines.append(
            f"  pass rate: bare {stats.get('control_pass_rate', 0):.0%} → "
            f"genesis {stats.get('treatment_pass_rate', 0):.0%}"
        )
    lines.append(
        f"  wins: genesis {stats.get('n_treatment_wins', 0)} / "
        f"bare {stats.get('n_control_wins', 0)} / "
        f"ties {stats.get('n_ties', stats.get('n_concordant_pass', 0) + stats.get('n_concordant_fail', 0))}"
    )
    rec = stats.get("recommendation", "")
    p = stats.get("p_value")
    p_txt = f", p={p:.3f}" if isinstance(p, (int, float)) else ""
    if rec == "insufficient_data":
        lines.append(
            f"  verdict: insufficient_data{p_txt} — PILOT: expected at N≤10; "
            "directional means only, do NOT quote as significant"
        )
    else:
        lines.append(f"  verdict: {rec}{p_txt}")
    return "\n".join(lines)


def render_console(report: BenchReport) -> str:
    """Human-readable summary for the CLI."""
    lines = [
        "=" * 64,
        f"BENCH {report.run_id} — genesis vs bare ({report.model}/{report.effort})",
        f"task set {report.task_set_version} sha256={report.task_file_sha256[:12]}…",
        f"judge: {report.rubric_name} v{report.rubric_version} "
        f"[judge_calibrated: {str(report.judge_calibrated).lower()}]",
        "=" * 64,
    ]
    for pair in report.pairs:
        if pair.skipped:
            reasons = "; ".join(
                f"{o.arm}: {o.skip_reason}"
                for o in (pair.bare, pair.genesis) if o.skipped
            )
            lines.append(f"  {pair.task.id:<24} SKIP ({reasons})")
        else:
            lines.append(
                f"  {pair.task.id:<24} bare {pair.bare.judge_score:.2f}  "
                f"genesis {pair.genesis.judge_score:.2f}  ({pair.task.category})"
            )
    complete = sum(1 for p in report.pairs if not p.skipped)
    lines.append("-" * 64)
    lines.append(f"pairs: {complete} complete / {len(report.pairs)} total")
    lines.append("score win-rate (headline):")
    lines.append(_fmt_stat(report.score_winrate))
    lines.append("pass win-rate (secondary):")
    lines.append(_fmt_stat(report.pass_winrate))
    if report.prod_delta:
        clean = report.prod_delta.get("clean")
        mark = "CLEAN" if clean else "DELTA — attribution required (live prod has ambient writes)"
        lines.append(f"prod isolation probe: {mark}")
        for d in report.prod_delta.get("deltas", []):
            lines.append(f"    {d}")
    for note in report.notes:
        lines.append(f"note: {note}")
    if report.control_run_id:
        lines.append(
            f"persisted: control={report.control_run_id} "
            f"treatment={report.treatment_run_id} (linked via comparison_run_id)"
        )
    lines.append("=" * 64)
    return "\n".join(lines)
