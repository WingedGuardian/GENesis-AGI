#!/usr/bin/env python3
"""Prepare a private complete source packet; this never writes a verification row."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genesis.session_awareness.pr_verification_sources import (  # noqa: E402
    ACQUISITION_SECONDS,
    SourceError,
    acquire_sources,
    reconcile_diff,
    save_packet,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="acquire body, complete diff and file census")
    prepare.add_argument("--repo", required=True)
    prepare.add_argument("--pr", required=True, type=int)
    prepare.add_argument(
        "--output", required=True, type=Path, help="new private directory; parent must exist"
    )
    args = parser.parse_args(argv)
    try:
        snapshot = acquire_sources(args.repo, args.pr)
        # Each path a check could not cover, named so the reviewer treats it as a
        # gap; derived from the packet's own diff and census, so not stored.
        gaps = reconcile_diff(snapshot.diff.encode(), snapshot.files, timeout=ACQUISITION_SECONDS)
        packet = save_packet(snapshot, args.output)
    except (SourceError, OSError) as exc:
        # Do not dump subprocess stderr, PR bodies or raw source on failure.
        print(f"verification_review: no ready packet: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"Prepared {args.repo}#{args.pr}: {len(snapshot.files)} files; {packet}")
    for kind, label in (("blob", "Blob identity"), ("counts", "Line counts (binary)")):
        if gaps[kind]:
            print(f"{label} not comparable for {len(gaps[kind])} path(s):")
            for path in gaps[kind]:
                print(f"  {path!r}")
    print("Independent completeness review is still required; no ledger write occurred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
