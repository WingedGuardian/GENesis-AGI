#!/usr/bin/env python3
"""One-time backfill: classify existing memories and update Qdrant payloads.

Safe to run while the system is live — uses set_payload (merges, no re-embedding).
Idempotent — memories already classified are counted but not changed.

Usage:
    source .venv/bin/activate
    python scripts/backfill_memory_class.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from genesis.memory.classification import classify_memory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill memory_class on Qdrant payloads")
    parser.add_argument("--dry-run", action="store_true", help="Print classifications without updating")
    args = parser.parse_args()

    import httpx

    qdrant_url = "http://localhost:6333"
    collection = "episodic_memory"

    # Scroll all points
    offset = None
    total = 0
    classified = {"rule": 0, "fact": 0, "reference": 0}
    already_set = 0

    while True:
        body: dict = {"limit": 100, "with_payload": True}
        if offset is not None:
            body["offset"] = offset

        resp = httpx.post(f"{qdrant_url}/collections/{collection}/points/scroll", json=body)
        resp.raise_for_status()
        data = resp.json()["result"]
        points = data.get("points", [])

        if not points:
            break

        for point in points:
            payload = point.get("payload", {})
            content = payload.get("content", "")
            source = payload.get("source", "")
            pipeline = payload.get("source_pipeline", "")
            existing_class = payload.get("memory_class")

            if existing_class:
                already_set += 1
                total += 1
                continue

            mem_class = classify_memory(content, source=source, source_pipeline=pipeline)
            classified[mem_class] += 1
            total += 1

            if not args.dry_run:
                # Merge memory_class into existing payload
                httpx.post(
                    f"{qdrant_url}/collections/{collection}/points/payload",
                    json={
                        "payload": {"memory_class": mem_class},
                        "points": [str(point["id"])],
                    },
                ).raise_for_status()

            if total % 100 == 0:
                print(f"  Processed {total} points...", file=sys.stderr)

        offset = data.get("next_page_offset")
        if offset is None:
            break

    action = "Would classify" if args.dry_run else "Classified"
    print(f"\n{action} {total} memories in {collection}:")
    print(f"  rule:      {classified['rule']}")
    print(f"  fact:      {classified['fact']}")
    print(f"  reference: {classified['reference']}")
    print(f"  already:   {already_set}")


if __name__ == "__main__":
    main()
