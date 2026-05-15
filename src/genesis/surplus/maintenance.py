"""Infrastructure maintenance surplus executors.

All Tier 1 — autonomous, observation-only, no side effects beyond cleanup
within a strict allowlist. Each executor produces an observation or alert;
the user decides what to act on.

Task types handled:
- DISK_CLEANUP: scan allowlisted paths, report reclaimable bytes
- BACKUP_VERIFICATION: check backup age, alert if stale
- DEAD_LETTER_REPLAY: retry failed DLQ items via existing redispatch
- DB_MAINTENANCE: report DB size, row counts, table stats (advise only)
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.surplus.types import ExecutorResult, SurplusTask

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.dead_letter import DeadLetterQueue
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# ─── Disk Cleanup ────────────────────────────────────────────────────────

# STRICT ALLOWLIST — only these patterns are eligible for cleanup.
# Never touches repo, data/, config/, or user files.
_CLEANUP_RULES: list[dict] = [
    {
        "description": "Genesis logs older than 7 days",
        "base": Path.home() / ".genesis" / "logs",
        "pattern": "*.log",
        "max_age_days": 7,
        "action": "report",  # Tier 1: report only, never delete
    },
    {
        "description": "Background session transcripts older than 30 days",
        "base": Path.home() / ".genesis" / "background-sessions",
        "pattern": "*.jsonl",
        "max_age_days": 30,
        "action": "report",
    },
]


class DiskCleanupExecutor:
    """Scan allowlisted paths and report reclaimable space.

    Tier 1 (observation-only): reports what COULD be cleaned, never deletes.
    A Tier 2 gate would be needed for actual deletion.
    """

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        cutoff_now = datetime.now(UTC)
        total_bytes = 0
        file_count = 0
        lines: list[str] = ["Disk cleanup scan (observation only):"]

        for rule in _CLEANUP_RULES:
            base: Path = rule["base"]
            if not base.is_dir():
                continue

            pattern: str = rule["pattern"]
            max_age = timedelta(days=rule["max_age_days"])
            cutoff = cutoff_now - max_age
            rule_bytes = 0
            rule_files = 0

            for f in base.glob(pattern):
                if not f.is_file():
                    continue
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
                    if mtime < cutoff:
                        size = f.stat().st_size
                        rule_bytes += size
                        rule_files += 1
                except OSError:
                    continue

            if rule_files > 0:
                total_bytes += rule_bytes
                file_count += rule_files
                lines.append(
                    f"  {rule['description']}: {rule_files} files, "
                    f"{_fmt_bytes(rule_bytes)}"
                )

        if file_count == 0:
            lines.append("  Nothing to clean — all paths within retention.")

        lines.append(f"  Total reclaimable: {_fmt_bytes(total_bytes)} ({file_count} files)")
        content = "\n".join(lines)

        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": "maintenance",
                "drive_alignment": task.drive_alignment,
                "confidence": 1.0,
                "reclaimable_bytes": total_bytes,
                "reclaimable_files": file_count,
            }],
        )


# ─── Backup Verification ────────────────────────────────────────────────

class BackupVerificationExecutor:
    """Check backup recency and alert if stale.

    Looks at the backup script's last-run marker or the backup
    repo's most recent commit timestamp.
    """

    def __init__(self, *, max_age_hours: int = 24) -> None:
        self._max_age_hours = max_age_hours

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        status_file = Path.home() / ".genesis" / "backup_status.json"
        lines: list[str] = ["Backup verification:"]
        stale = False

        if status_file.exists():
            try:
                data = json.loads(status_file.read_text())
                ts_str = data.get("timestamp", "")
                success = data.get("success", False)
                last_backup = datetime.fromisoformat(ts_str)
                age = datetime.now(UTC) - last_backup
                age_hours = age.total_seconds() / 3600

                lines.append(f"  Last backup: {ts_str} ({age_hours:.1f}h ago)")
                lines.append(f"  Success: {success}")

                if data.get("duration_s"):
                    lines.append(f"  Duration: {data['duration_s']}s")
                if data.get("sqlite_lines"):
                    lines.append(f"  SQLite rows: {data['sqlite_lines']:,}")
                if data.get("qdrant_collections") is not None:
                    lines.append(f"  Qdrant collections: {data['qdrant_collections']}")
                if data.get("secrets_encrypted") is not None:
                    lines.append(f"  Secrets encrypted: {data['secrets_encrypted']}")

                if not success:
                    stale = True
                    reason = data.get("failure_reason") or "unknown"
                    lines.append(f"  WARNING: Last backup FAILED — {reason}")
                elif age_hours > self._max_age_hours:
                    stale = True
                    lines.append(
                        f"  WARNING: Backup is {age_hours:.0f}h old "
                        f"(threshold: {self._max_age_hours}h)"
                    )
                else:
                    lines.append("  Status: OK (within retention window)")
            except (ValueError, OSError, json.JSONDecodeError, KeyError) as e:
                lines.append(f"  Error reading backup status: {e}")
                stale = True
        else:
            lines.append(f"  Backup status file not found: {status_file}")
            stale = True

        content = "\n".join(lines)
        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": "maintenance",
                "drive_alignment": task.drive_alignment,
                "confidence": 1.0,
                "backup_stale": stale,
            }],
        )


# ─── Dead Letter Replay ─────────────────────────────────────────────────

class DeadLetterReplayExecutor:
    """Replay pending dead-letter items via the existing router.

    Uses DeadLetterQueue.redispatch() which handles all the parsing,
    message recovery, and status tracking.
    """

    def __init__(
        self, *, dead_letter: DeadLetterQueue, router: Router,
    ) -> None:
        self._dlq = dead_letter
        self._router = router

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        pending = await self._dlq.get_pending_count()
        if pending == 0:
            return ExecutorResult(
                success=True,
                content="Dead letter queue empty — nothing to replay.",
                insights=[{
                    "content": "DLQ empty, no replay needed.",
                    "source_task_type": task.task_type,
                    "generating_model": "maintenance",
                    "drive_alignment": task.drive_alignment,
                    "confidence": 1.0,
                    "dlq_pending": 0,
                }],
            )

        # Expire stale items first
        expired = await self._dlq.expire_old()

        # Redispatch via the router
        succeeded, failed = await self._dlq.redispatch(self._router.route_call)

        remaining = await self._dlq.get_pending_count()
        content = (
            f"Dead letter replay: {succeeded} succeeded, {failed} failed, "
            f"{expired} expired. {remaining} remaining."
        )

        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": "maintenance",
                "drive_alignment": task.drive_alignment,
                "confidence": 1.0,
                "dlq_succeeded": succeeded,
                "dlq_failed": failed,
                "dlq_expired": expired,
                "dlq_remaining": remaining,
            }],
        )


# ─── DB Maintenance ──────────────────────────────────────────────────────

class DbMaintenanceExecutor:
    """Report database size, row counts, and health stats.

    Tier 3 for actual maintenance (VACUUM needs exclusive lock).
    This executor only REPORTS — never modifies the DB.
    """

    def __init__(self, *, db: aiosqlite.Connection) -> None:
        self._db = db

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        from genesis.env import genesis_db_path

        lines: list[str] = ["Database maintenance report:"]

        # DB file size
        db_path = genesis_db_path()
        try:
            db_size = db_path.stat().st_size
            lines.append(f"  DB file size: {_fmt_bytes(db_size)}")
        except OSError:
            lines.append(f"  DB file: {db_path} (could not stat)")

        # WAL size
        wal_path = db_path.parent / (db_path.name + "-wal")
        if wal_path.exists():
            try:
                wal_size = wal_path.stat().st_size
                lines.append(f"  WAL size: {_fmt_bytes(wal_size)}")
            except OSError:
                pass

        # Table row counts for key tables
        key_tables = [
            "observations", "memories", "surplus_queue", "surplus_insights",
            "dead_letter", "eval_runs", "eval_results", "cc_sessions",
            "provider_activity",
        ]
        lines.append("  Table row counts:")
        for table in key_tables:
            try:
                cursor = await self._db.execute(
                    f"SELECT COUNT(*) FROM {table}"  # noqa: S608 — table names from hardcoded list
                )
                row = await cursor.fetchone()
                count = row[0] if row else 0
                lines.append(f"    {table}: {count:,}")
            except Exception:
                # Table may not exist yet
                pass

        # Integrity check (quick)
        try:
            cursor = await self._db.execute("PRAGMA quick_check(1)")
            result = await cursor.fetchone()
            status = result[0] if result else "unknown"
            lines.append(f"  Integrity: {status}")
        except Exception as e:
            lines.append(f"  Integrity check failed: {e}")

        content = "\n".join(lines)
        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": content,
                "source_task_type": task.task_type,
                "generating_model": "maintenance",
                "drive_alignment": task.drive_alignment,
                "confidence": 1.0,
            }],
        )


# ─── Transcript Archival ────────────────────────────────────────────────


async def archive_old_transcripts(
    base_dir: Path,
    *,
    older_than_days: int = 90,
) -> int:
    """Gzip .jsonl transcript files older than *older_than_days*.

    Lossless and reversible — originals are deleted only after a
    successful gzip write with size verification. Returns count of
    files archived.
    """
    import gzip

    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    archived = 0

    if not base_dir.is_dir():
        return 0

    for f in base_dir.glob("*.jsonl"):
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
            if mtime >= cutoff:
                continue

            gz_path = f.with_suffix(".jsonl.gz")
            original_size = f.stat().st_size

            # Write compressed
            with f.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                while chunk := src.read(65536):
                    dst.write(chunk)

            # Verify gzip wrote something reasonable
            if gz_path.stat().st_size == 0 and original_size > 0:
                gz_path.unlink(missing_ok=True)
                logger.warning("Gzip produced empty file for %s — skipped", f.name)
                continue

            # Safe to remove original
            f.unlink()
            archived += 1
        except OSError:
            logger.warning("Failed to archive %s", f.name, exc_info=True)
            continue

    return archived


# ─── Helpers ─────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"
