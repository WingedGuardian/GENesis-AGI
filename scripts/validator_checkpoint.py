#!/usr/bin/env python3
"""Read-only validator preflight and routing audit, without session registration."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import TypeAdapter, ValidationError  # noqa: E402

from genesis.session_awareness.pr_evidence import (  # noqa: E402
    MAX_EVIDENCE_BYTES,
    EvidenceError,
    Repo,
    parse_evidence,
)
from genesis.session_awareness.pr_recording import _no_duplicates  # noqa: E402
from genesis.session_awareness.pr_tracking import _gh_json, assess_tracking  # noqa: E402


def _offline(*args):
    raise LookupError("offline: live public receipt readback still required")


_LEGACY_ENTRIES = "Legacy evidence: {n} entries in the retired scope_limits field have no gap kind; "
_LEGACY_EMPTY = "Legacy evidence: written with the retired scope_limits field (empty); "
# What a validator can do next depends on the row: an open one is re-recorded in the
# current schema; a closed one can never be amended, so the routing goes elsewhere.
_LEGACY_NEXT = {
    "open": "re-record it with gaps, choosing each entry's kind (method, event_gated, owner_only, not_yet_done)",
    "closed": "closed rows are never amended: classify each entry's gap kind in the run log "
              "and file or reuse tracking for any actionable one",
}


def _legacy_pending(entries: int, status: str) -> str:
    head = _LEGACY_ENTRIES.format(n=entries) if entries else _LEGACY_EMPTY
    return head + _LEGACY_NEXT.get(status, _LEGACY_NEXT["closed"])


def _legacy_document(data):
    """Read evidence written before ``scope_limits`` was replaced by ``gaps``, for this report only.

    Returns ``(document, entry_count)`` when *data* is otherwise a valid evidence
    document carrying a ``scope_limits`` list of strings, else ``None``. The entries
    are counted, never mapped to gap kinds: the kind decides the verdict, so the
    writers still refuse ``scope_limits`` and a row read this way is never complete.
    """
    if not isinstance(data, dict) or "gaps" in data:
        return None  # a document with gaps was written after the change: not legacy
    limits = data.get("scope_limits")
    if not isinstance(limits, list) or not all(isinstance(entry, str) for entry in limits):
        return None
    try:
        doc = parse_evidence({key: value for key, value in data.items() if key != "scope_limits"})
    except EvidenceError:
        return None
    return doc, len(limits)


async def audit(db, repo: str, prs: list[int] | None, *, offline: bool = False) -> dict:
    """Include closed legacy debt. Close SELECT cursors before network reads.

    Enumerate only keys up front; retaining a live read transaction during every
    GitHub call would pin the WAL for the whole audit. Each row is a fresh read,
    not a claim of an atomic snapshot across an active validator's updates.
    """
    if prs is None:
        async with db.execute("SELECT pr_number FROM pr_verifications WHERE repo = ? ORDER BY pr_number", (repo,)) as cur:
            keys = [row[0] for row in await cur.fetchall()]
    else:
        keys = sorted(set(prs))
    results = []
    for pr in keys:
        async with db.execute(
            "SELECT status, verdict, evidence FROM pr_verifications WHERE repo = ? AND pr_number = ?",
            (repo, pr),
        ) as cur:
            row = await cur.fetchone()
        item = {"repo": repo, "pr": pr, "complete": False}
        if row is None:
            results.append(item | {"pending": ["No verification row exists"]})
            continue
        item.update(status=row[0], verdict=row[1])
        legacy = None
        try:
            raw = row[2]
            if not isinstance(raw, str) or not raw:
                raise ValueError("no evidence")
            if len(raw.encode("utf-8")) > MAX_EVIDENCE_BYTES:
                raise ValueError("over evidence cap")
            data = json.loads(raw, object_pairs_hook=_no_duplicates)
            try:
                doc = parse_evidence(data)
            except EvidenceError:
                if (legacy := _legacy_document(data)) is None:
                    raise
                doc = legacy[0]
            if doc.repo != repo or doc.pr != pr:
                raise ValueError("evidence does not belong to this row")
        except (ValueError, TypeError, RecursionError, EvidenceError):
            # Never dump private evidence, schema inputs or exception messages.
            results.append(item | {"pending": ["Missing, malformed or mismatched historical evidence; manual classification required"]})
            continue
        report = (await assess_tracking(doc, db=db, read_public=_offline if offline else None)).as_dict()
        if legacy is not None:
            report |= {"complete": False, "legacy_scope_limits": legacy[1],
                       "pending": [_legacy_pending(legacy[1], row[0]), *report["pending"]]}
        results.append(item | report)
    return {"complete": all(item["complete"] for item in results), "rows": results,
            "read_only": True, "offline": offline, "snapshot": "per row"}


async def preflight(repo: str, db_path: Path) -> dict:
    """Probe CLI capabilities. MCP clients discover their actual exposed tools."""
    public = {"available": False, "reason": "gh unavailable"}
    if shutil.which("gh"):
        try:
            metadata = await asyncio.to_thread(_gh_json, f"repos/{repo}")
            permissions = metadata.get("permissions") or {}
            available = (metadata.get("private") is False and isinstance(permissions, dict)
                         and permissions.get("push") is True)
            public = {"available": available,
                      "reason": "Use privacy-scanned file_tracker_issue.py" if available
                      else "Public tracker/write permission not established"}
        except LookupError as exc:
            public = {"available": False, "reason": str(exc)}
    return {
        "client": "cli", "repo": repo, "public_writer": public,
        "private_writer": {"available": False, "reason": "CLI has no private task writer; discover an explicitly authorized MCP writer if needed"},
        "database_exists": db_path.is_file(),
        "authorization": "Tool availability does not expand session authorization",
    }


async def run(args) -> dict:
    if args.command == "preflight":
        return await preflight(args.repo, args.db_path)
    from genesis.db.connection import open_ro_connection

    db = await open_ro_connection(args.db_path)
    try:
        report = await audit(db, args.repo, args.pr, offline=args.offline or args.pause)
    finally:
        await db.close()
    return report | {"paused": args.pause}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    from genesis.env import genesis_db_path

    for command in ("preflight", "audit", "checkpoint"):
        child = sub.add_parser(command)
        child.add_argument("--repo", required=True, help="Exact owner/repository tracker")
        child.add_argument("--db-path", type=Path, default=genesis_db_path())
        if command != "preflight":
            child.add_argument("--pr", type=int, action="append", required=command == "checkpoint")
            child.add_argument("--offline", action="store_true", help="Report structural debt; never certify unread public receipts")
            child.add_argument("--pause", action="store_true", help="Honor pause without network reads (exit 0), retaining incomplete status")
    args = parser.parse_args(argv)
    try:
        TypeAdapter(Repo).validate_python(args.repo)
    except ValidationError:
        parser.error("--repo must be an unpadded owner/repository slug")
    if any(not 1 <= pr <= 2**63 - 1 for pr in (getattr(args, "pr", None) or [])):
        parser.error("--pr must be in 1..2**63-1")
    try:
        report = asyncio.run(run(args))
    except (sqlite3.Error, OSError, RuntimeError):
        print(json.dumps({"complete": False, "read_only": True,
                          "error": "Audit unavailable; no measurement or routing state was changed"}))
        return 0 if getattr(args, "pause", False) else 1
    print(json.dumps(report, indent=2))
    return 0 if report.get("paused") or report.get("complete", args.command == "preflight") else 4


if __name__ == "__main__":
    sys.exit(main())
