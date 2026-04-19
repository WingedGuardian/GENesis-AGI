#!/usr/bin/env python3
"""Phase 6 — audit observation writer for post-commit hook.

Stdlib-only. Safe to run without the Genesis venv activated. Called by
scripts/hooks/post-commit after a `fix:` commit lands. Writes a
`bugfix_committed` row to the observations table for durability/audit.

Fail-open: any error is logged to stderr (which the hook redirects to
~/.genesis/contribution-hook.log) and the process exits 0 so nothing
blocks the git flow.

Usage:
    emit_bugfix_audit.py <commit_sha> <commit_subject>
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _db_path() -> Path:
    """Resolve the Genesis DB path without importing genesis.env.

    Genesis stores its DB at ~/genesis/data/genesis.db by default.
    GENESIS_DB_PATH env override is honored.
    """
    override = os.environ.get("GENESIS_DB_PATH")
    if override:
        return Path(override)
    return Path.home() / "genesis" / "data" / "genesis.db"


def _content_hash(source: str, content: str) -> str:
    """Compute a stable content hash for dedup.

    Matches the column semantics of observations.content_hash
    (indexed on (source, content_hash)).
    """
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(b"\x00")
    h.update(content.encode("utf-8"))
    return h.hexdigest()


def emit(sha: str, subject: str) -> int:
    db = _db_path()
    if not db.exists():
        print(f"emit_bugfix_audit: db not found at {db}, skipping", file=sys.stderr)
        return 0  # fail-open

    content = f"Bug fix committed: {sha[:12]} — {subject}"
    source = "post_commit_hook"
    obs_type = "bugfix_committed"
    chash = _content_hash(source, content)
    now = datetime.now(UTC).isoformat()
    obs_id = str(uuid.uuid4())

    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            # Check dedup before insert — idempotent if same commit triggers twice.
            existing = conn.execute(
                "SELECT id FROM observations WHERE source = ? AND content_hash = ? LIMIT 1",
                (source, chash),
            ).fetchone()
            if existing:
                print(f"emit_bugfix_audit: dedup hit for sha={sha[:12]}", file=sys.stderr)
                return 0

            # Compute TTL inline (can't use async CRUD in sync hook).
            # bugfix_committed → 30-day TTL, matching observations._TTL_BY_TYPE.
            expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()

            conn.execute(
                """
                INSERT INTO observations
                    (id, source, type, category, content, priority,
                     created_at, content_hash, expires_at)
                VALUES
                    (?,  ?,      ?,    ?,        ?,       ?,
                     ?,          ?,            ?)
                """,
                (
                    obs_id,
                    source,
                    obs_type,
                    "contribution",  # category
                    content,
                    "low",  # audit only, not actionable on its own
                    now,
                    chash,
                    expires_at,
                ),
            )
            conn.commit()
            print(f"emit_bugfix_audit: wrote obs {obs_id} for sha={sha[:12]}", file=sys.stderr)
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"emit_bugfix_audit: sqlite error: {e}", file=sys.stderr)
        return 0  # fail-open
    except Exception as e:  # last resort — never block the commit
        print(f"emit_bugfix_audit: unexpected error: {e}", file=sys.stderr)
        return 0

    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: emit_bugfix_audit.py <sha> <subject>", file=sys.stderr)
        return 0  # fail-open, wrong usage shouldn't break commits
    return emit(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    sys.exit(main())
