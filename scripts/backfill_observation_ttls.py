#!/usr/bin/env python3
"""One-time backfill: set expires_at on unresolved observations with NULL TTL.

Uses the _TTL_BY_TYPE and _PERMANENT_TYPES from observations.py to compute
expires_at = created_at + TTL. If the computed expires_at is already in the
past, resolves the observation immediately.

Also bulk-resolves known stale observation types:
- cc_version_available (all)
- genesis_version_change (>7 days)
- memory_index (all)
- memory_operation (all)
- version_change (>3 days)

Safe to run multiple times — idempotent (only targets NULL expires_at).

Usage:
    python scripts/backfill_observation_ttls.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add src to path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill observation TTLs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    args = parser.parse_args()

    import sqlite3

    from genesis.db.crud.observations import (
        _DEFAULT_TTL,
        _PERMANENT_TYPES,
        _TTL_BY_TYPE,
        _TTL_PREFIX,
    )

    db_path = Path.home() / "genesis" / "data" / "genesis.db"
    if not db_path.exists():
        print(f"Database not found at {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    # ── Phase 1: Bulk resolve known stale types ───────────────────────
    stale_rules = [
        ("cc_version_available", None, "unwired — Genesis pegged to specific CC version"),
        ("memory_index", None, "mechanical — harvested periodically"),
        ("memory_operation", None, "mechanical — ephemeral"),
    ]
    time_based_rules = [
        ("genesis_version_change", 7, "version tracking — 7-day TTL"),
        ("version_change", 3, "version tracking — 3-day TTL"),
    ]

    total_resolved = 0
    for obs_type, _, reason in stale_rules:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE type = ? AND resolved = 0",
            (obs_type,),
        )
        count = cursor.fetchone()[0]
        if count > 0:
            if not args.dry_run:
                conn.execute(
                    "UPDATE observations SET resolved = 1, resolved_at = ?, "
                    "resolution_notes = ? WHERE type = ? AND resolved = 0",
                    (now_iso, f"bulk-resolved: {reason}", obs_type),
                )
            total_resolved += count
            print(f"  Resolved {count} {obs_type} — {reason}")

    for obs_type, days, reason in time_based_rules:
        cutoff = (now - timedelta(days=days)).isoformat()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE type = ? AND resolved = 0 AND created_at < ?",
            (obs_type, cutoff),
        )
        count = cursor.fetchone()[0]
        if count > 0:
            if not args.dry_run:
                conn.execute(
                    "UPDATE observations SET resolved = 1, resolved_at = ?, "
                    "resolution_notes = ? "
                    "WHERE type = ? AND resolved = 0 AND created_at < ?",
                    (now_iso, f"bulk-resolved: {reason}", obs_type, cutoff),
                )
            total_resolved += count
            print(f"  Resolved {count} {obs_type} (>{days}d) — {reason}")

    print(f"\nPhase 1: {total_resolved} stale observations resolved")

    # ── Phase 2: Backfill TTLs on NULL-expiry observations ────────────
    cursor = conn.execute(
        "SELECT id, type, created_at FROM observations "
        "WHERE resolved = 0 AND expires_at IS NULL"
    )
    rows = list(cursor.fetchall())
    print(f"\nPhase 2: {len(rows)} unresolved observations with NULL expires_at")

    backfilled = 0
    auto_resolved = 0
    permanent_kept = 0

    for row in rows:
        obs_id = row["id"]
        obs_type = row["type"]
        created_at = row["created_at"]

        # Check permanent
        if obs_type in _PERMANENT_TYPES:
            permanent_kept += 1
            continue

        # Compute TTL
        ttl = _TTL_BY_TYPE.get(obs_type)
        if ttl is None:
            for prefix, prefix_ttl in _TTL_PREFIX:
                if obs_type.startswith(prefix):
                    ttl = prefix_ttl
                    break
        if ttl is None:
            ttl = _DEFAULT_TTL

        # Compute expires_at
        try:
            created_dt = datetime.fromisoformat(created_at)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
            expires_at = (created_dt + ttl).isoformat()
        except (ValueError, TypeError):
            continue

        # If already expired, resolve immediately
        if expires_at < now_iso:
            if not args.dry_run:
                conn.execute(
                    "UPDATE observations SET resolved = 1, resolved_at = ?, "
                    "resolution_notes = 'auto-expired (TTL backfill)', "
                    "expires_at = ? WHERE id = ?",
                    (now_iso, expires_at, obs_id),
                )
            auto_resolved += 1
        else:
            if not args.dry_run:
                conn.execute(
                    "UPDATE observations SET expires_at = ? WHERE id = ?",
                    (expires_at, obs_id),
                )
            backfilled += 1

    if not args.dry_run:
        conn.commit()

    print(f"  Backfilled: {backfilled} (set expires_at for future expiry)")
    print(f"  Auto-resolved: {auto_resolved} (already past computed TTL)")
    print(f"  Permanent: {permanent_kept} (kept without TTL)")

    # ── Summary ───────────────────────────────────────────────────────
    # Verify remaining NULL-expiry count
    cursor = conn.execute(
        "SELECT COUNT(*) FROM observations "
        "WHERE resolved = 0 AND expires_at IS NULL"
    )
    remaining = cursor.fetchone()[0]
    print(f"\nRemaining NULL-expiry unresolved: {remaining}")

    if remaining > 0:
        cursor = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM observations "
            "WHERE resolved = 0 AND expires_at IS NULL "
            "GROUP BY type ORDER BY cnt DESC LIMIT 10"
        )
        print("  By type:")
        for r in cursor.fetchall():
            print(f"    {r['type']}: {r['cnt']}")

    conn.close()

    if args.dry_run:
        print("\n[DRY RUN — no changes made]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
