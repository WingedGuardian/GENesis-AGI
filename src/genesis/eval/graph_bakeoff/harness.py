"""Bake-off harness orchestration (S1 scope: parity of the NX control vs oracle).

``run_parity`` loads the NX control against a snapshot, resolves the frozen
anchors, and for every ``exact_set`` query compares the control's reachable
node-id set to the SQL oracle. A green parity report is the S1 gate and the
precondition for ``bench time`` in S3.
"""

from __future__ import annotations

import sqlite3

from . import parity
from .anchors import resolve_anchors
from .base import QueryResult
from .engines.nx_incremental import NxIncrementalEngine
from .queries import EXACT_SET, QUERIES, QuerySpec


async def run_parity(snapshot_path: str, *, anchors: dict | None = None) -> dict:
    """Load the NX control + compare every exact_set query to the SQL oracle.

    Returns {"all_green": bool, "verdicts": [...], "load": {...}}.
    """
    anchors = anchors if anchors is not None else resolve_anchors(snapshot_path)

    engine = NxIncrementalEngine()
    load = await engine.load(snapshot_path)
    engine.anchors = anchors

    conn = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    verdicts: list[dict] = []
    try:
        for q in QUERIES:
            if q.parity_mode != EXACT_SET:
                verdicts.append(
                    {
                        "query": q.id,
                        "parity_mode": q.parity_mode,
                        "skipped": "not exact_set (S2/S3)",
                    }
                )
                continue
            oracle_set = parity.oracle_for(q.id, conn, anchors, q.params)
            result: QueryResult = await engine.run(q, q.params)
            cmp = parity.compare_exact_set(oracle_set or frozenset(), result.node_ids)
            cmp.update(
                {
                    "query": q.id,
                    "latency_scored": q.latency_scored,
                    "run_ms": round(result.run_ms, 1),
                }
            )
            verdicts.append(cmp)
    finally:
        conn.close()

    exact = [v for v in verdicts if "match" in v]
    return {
        "all_green": all(v["match"] for v in exact),
        "n_exact": len(exact),
        "n_matched": sum(1 for v in exact if v["match"]),
        "verdicts": verdicts,
        "load": {"nodes": load.nodes, "edges": load.edges, "load_ms": round(load.load_ms, 1)},
    }


def format_report(report: dict) -> str:
    lines = [
        f"NX-control vs SQL-oracle parity  ({report['n_matched']}/{report['n_exact']} exact_set queries green)",
        f"load: {report['load']['nodes']} nodes / {report['load']['edges']} edges in {report['load']['load_ms']}ms",
        "",
    ]
    for v in report["verdicts"]:
        if "match" in v:
            mark = "PASS" if v["match"] else "FAIL"
            tail = (
                ""
                if v["match"]
                else f"  missing={v['missing_n']} extra={v['extra_n']} {v['sample_missing'] or v['sample_extra']}"
            )
            lines.append(
                f"  [{mark}] {v['query']:<26} oracle={v['oracle_n']:<6} engine={v['engine_n']:<6} {v['run_ms']}ms{tail}"
            )
        else:
            lines.append(f"  [skip] {v['query']:<26} {v.get('skipped', '')}")
    lines.append("")
    lines.append("ALL GREEN" if report["all_green"] else "PARITY FAILURES PRESENT")
    return "\n".join(lines)


def get_engine(name: str):
    """Return an engine instance by name (control in-process; contenders are S2)."""
    if name == "nx_incremental":
        return NxIncrementalEngine()
    raise ValueError(f"engine {name!r} not available in-process (contenders run via S2 subprocess)")


__all__ = ["run_parity", "format_report", "get_engine", "QuerySpec"]
