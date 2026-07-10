#!/usr/bin/env python3
"""One-off: create payload indexes on the live Qdrant collections.

The server creates them at next init via ensure_collections(); this
script applies them immediately (online, non-destructive — Qdrant
builds payload indexes without blocking reads/writes) and prints the
resulting payload schema per collection.

Usage:
    python scripts/apply_qdrant_payload_indexes.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> None:
    from genesis.qdrant.collections import (
        COLLECTIONS,
        ensure_payload_indexes,
        get_client,
    )

    client = get_client()
    ensure_payload_indexes(client)
    for name in COLLECTIONS:
        info = client.get_collection(name)
        schema = {
            field: str(meta.data_type)
            for field, meta in (info.payload_schema or {}).items()
        }
        print(f"{name}: {json.dumps(schema, indent=2, sort_keys=True)}")


if __name__ == "__main__":
    main()
