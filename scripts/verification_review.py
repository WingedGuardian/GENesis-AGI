#!/usr/bin/env python3
"""Prepare a private complete source packet; this never writes a verification row."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genesis.session_awareness.pr_completeness import (  # noqa: E402
    assess_completeness,
    evidence_sha256,
    inventory_sha256,
    load_evidence,
    load_inventory,
    load_packet,
    validate_inventory,
)
from genesis.session_awareness.pr_evidence import EvidenceError  # noqa: E402
from genesis.session_awareness.pr_verification_sources import (  # noqa: E402
    ACQUISITION_SECONDS,
    SourceError,
    acquire_sources,
    reconcile_diff,
    save_packet,
)


def _check(args) -> None:
    snapshot = load_packet(args.packet)
    inventory = load_inventory(args.inventory)
    validate_inventory(inventory, snapshot)
    if args.evidence is not None:
        doc = load_evidence(args.evidence)
        if doc.completeness is not None and inventory_sha256(
            doc.completeness.inventory
        ) != inventory_sha256(inventory):
            raise SourceError("report inventory differs from the retained Stage-A inventory")
        result = assess_completeness(doc, snapshot)
        if not result.valid:
            raise SourceError(f"{result.status}: {result.reason}")
        print(
            "Review report structure and current bindings verified; execution evidence owns independence."
        )
    else:
        print(
            "Inventory structure and source bindings checked; semantic review remains unverified."
        )
    print(f"Inventory SHA-256: {inventory_sha256(inventory)}")
    print("Offline packet check: live source freshness was not checked.")
    print("Read-only check: nothing written.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="acquire body, complete diff and file census")
    prepare.add_argument("--repo", required=True)
    prepare.add_argument("--pr", required=True, type=int)
    prepare.add_argument(
        "--output", required=True, type=Path, help="new private directory; parent must exist"
    )
    prepare.add_argument(
        "--evidence",
        type=Path,
        help="optional candidate; print its canonical digest, keep it out of the source packet",
    )
    check = commands.add_parser(
        "check", help="check retained inventory and optional evidence review without writing"
    )
    check.add_argument(
        "--packet", required=True, type=Path, help="private directory containing packet.json"
    )
    check.add_argument("--inventory", required=True, type=Path, help="retained Stage-A JSON")
    check.add_argument(
        "--evidence", type=Path, help="candidate including its completed review report"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            _check(args)
            return 0
        # Validate/hash the candidate before publication; a bad candidate leaves
        # no packet and never contaminates the source-only Stage-A input.
        evidence_hash = evidence_sha256(load_evidence(args.evidence)) if args.evidence else None
        started = time.monotonic()
        snapshot = acquire_sources(args.repo, args.pr)
        # Each path a check could not cover, named so the reviewer treats it as a
        # gap; derived from the packet's own diff and census, so not stored.
        # One overall bound: the second pass gets only what acquisition left.
        remaining = ACQUISITION_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise SourceError("source acquisition deadline expired")
        gaps = reconcile_diff(snapshot.diff.encode(), snapshot.files, timeout=remaining)
        packet = save_packet(snapshot, args.output)
    except (SourceError, EvidenceError, OSError) as exc:
        # Do not dump subprocess stderr, PR bodies or raw source on failure.
        print(
            f"verification_review: {args.command} refused: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(f"Prepared {args.repo}#{args.pr}: {len(snapshot.files)} files; {packet}")
    for kind, label in (("blob", "Blob identity"), ("counts", "Line counts (binary)")):
        if gaps[kind]:
            print(f"{label} not comparable for {len(gaps[kind])} path(s):")
            for path in gaps[kind]:
                print(f"  {path!r}")
    print("Independent completeness review is still required; no ledger write occurred.")
    if evidence_hash is not None:
        print(f"Candidate evidence SHA-256: {evidence_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
