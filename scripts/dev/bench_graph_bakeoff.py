#!/usr/bin/env python3
"""Graph-engine bake-off CLI — manual dev tool, NOT run in CI.

Subcommands (S1): export | anchors | parity | smoke | all
  export  — snapshot the prod DB + write manifest (sha256 + counts)
  anchors — resolve + record the frozen anchors for a snapshot
  parity  — run NX-control vs SQL-oracle parity on all exact_set queries
  smoke   — report the contender smoke status (install/run proven separately)
  all     — export -> anchors -> parity

Runs in the PROD venv (has genesis + networkx 3.6.1). The contender engines
(ladybug/falkor) run in the throwaway venv as subprocesses — wired in S2.

Usage:
  python scripts/dev/bench_graph_bakeoff.py all --stamp 2026-08-06
  python scripts/dev/bench_graph_bakeoff.py parity --snapshot ~/tmp/graph-bakeoff/snapshot-2026-08-06.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from genesis.eval.graph_bakeoff import export as export_mod  # noqa: E402
from genesis.eval.graph_bakeoff import harness  # noqa: E402
from genesis.eval.graph_bakeoff.anchors import write_anchors  # noqa: E402

OUT = Path.home() / "tmp" / "graph-bakeoff"


def _default_snapshot(stamp: str) -> Path:
    return OUT / f"snapshot-{stamp}.db"


def cmd_export(args) -> int:
    manifest = export_mod.export_snapshot(
        stamp=args.stamp, out_dir=OUT, source=Path(args.source) if args.source else None
    )
    print(json.dumps(manifest, indent=2))
    return 0


def cmd_anchors(args) -> int:
    snap = Path(args.snapshot) if args.snapshot else _default_snapshot(args.stamp)
    if not snap.exists():
        print(f"snapshot not found: {snap} (run `export` first)", file=sys.stderr)
        return 1
    # Derive the hash from the SELECTED snapshot, never from OUT/manifest.json:
    # `--snapshot` may point at an older/external snapshot while manifest.json
    # holds the LATEST export's sha, which would mislabel anchors resolved from a
    # different DB (defeating the per-snapshot reproducibility guarantee). Reuse
    # export's canonical hasher rather than reinventing it.
    sha = export_mod._sha256(snap)
    dest = write_anchors(str(snap), sha, OUT)
    print(f"anchors -> {dest}")
    print(dest.read_text())
    return 0


def cmd_parity(args) -> int:
    snap = Path(args.snapshot) if args.snapshot else _default_snapshot(args.stamp)
    if not snap.exists():
        print(f"snapshot not found: {snap} (run `export` first)", file=sys.stderr)
        return 2
    report = asyncio.run(harness.run_parity(str(snap)))
    print(harness.format_report(report))
    return 0 if report["all_green"] else 1


def cmd_smoke(args) -> int:
    from genesis.eval.graph_bakeoff.engines.falkor import FalkorEngine
    from genesis.eval.graph_bakeoff.engines.ladybug import LadybugEngine

    for eng in (LadybugEngine, FalkorEngine):
        print(f"{eng.name:<10} available_in_this_venv={eng.available()}  {eng().stats()}")
    print("(contender install+run smoke PROVEN standalone 2026-08-06 in the throwaway venv)")
    return 0


def cmd_all(args) -> int:
    rc = cmd_export(args)
    if rc:
        return rc
    cmd_anchors(args)
    return cmd_parity(args)


def main() -> int:
    ap = argparse.ArgumentParser(description="Graph-engine bake-off (manual dev tool)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "anchors", "parity", "smoke", "all"):
        p = sub.add_parser(name)
        p.add_argument("--stamp", default="2026-08-06", help="snapshot date stamp")
        p.add_argument("--snapshot", default=None, help="explicit snapshot path")
        p.add_argument(
            "--source", default=None, help="explicit prod DB source (else genesis_db_path())"
        )
    args = ap.parse_args()
    return {
        "export": cmd_export,
        "anchors": cmd_anchors,
        "parity": cmd_parity,
        "smoke": cmd_smoke,
        "all": cmd_all,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
