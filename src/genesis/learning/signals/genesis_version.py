"""GenesisVersionCollector — detects Genesis repo updates available upstream.

Checks local HEAD against origin/main on a self-throttled interval
(default 6h, configurable via config/updates.yaml). When upstream has
new commits, stores a genesis_update_available observation and optionally
sends a Telegram notification via the outreach pipeline.

Also detects local version changes (e.g., after an update was applied)
and checks for update failure context files left by update.sh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import yaml

from genesis.awareness.types import SignalReading

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[4] / "config"
_GENESIS_ROOT = Path(__file__).resolve().parents[4]
_FAILURE_FILE = Path.home() / ".genesis" / "last_update_failure.json"
_FAILURE_ARCHIVE_DIR = Path.home() / ".genesis" / "update-failures"
# Keep only the N most recent archived failures. Older ones are pruned
# on each archive call so the directory can't grow unbounded.
_FAILURE_ARCHIVE_CAP = 10


def _load_updates_config() -> dict:
    """Load config/updates.yaml. Returns defaults if missing."""
    path = _CONFIG_DIR / "updates.yaml"
    if path.is_file():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {"check": {"enabled": True, "interval_hours": 6}}


class GenesisVersionCollector:
    """Detects Genesis version changes and available upstream updates.

    Self-throttled: only runs ``git fetch`` every ``interval_hours``
    (from config/updates.yaml). On intermediate awareness ticks, the
    collector skips the fetch and relies on stored observations to
    drive dashboard alerts.

    When upstream commits are detected:
    - Stores ``genesis_update_available`` observation (deduped by target commit)
    - Sends outreach alert if notify.enabled is true
    - Emits signal value=1.0

    Also monitors for:
    - Local HEAD changes (after update applied) — resolves prior
      ``genesis_update_available`` observations so the alert clears
    - Update failure context files from update.sh rollback — archives
      processed file with a timestamp suffix under
      ``~/.genesis/update-failures/`` so evidence is preserved across
      repeated failures and the live file isn't re-read on every tick.
      Archive directory is capped at ``_FAILURE_ARCHIVE_CAP`` entries;
      oldest are pruned on each archive call.
    """

    signal_name = "genesis_version_changed"

    def __init__(
        self,
        db: aiosqlite.Connection,
        pipeline_getter: object | None = None,
    ) -> None:
        self._db = db
        self._pipeline_getter = pipeline_getter
        self._last_fetch_at: datetime | None = None

    async def collect(self) -> SignalReading:
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        config = _load_updates_config()
        if not config.get("check", {}).get("enabled", True):
            return SignalReading(
                name=self.signal_name, value=0.0,
                source="genesis_version", collected_at=now_iso,
            )

        # ── Check for update failure file ────────────────────────────
        try:
            await self._check_failure_file()
        except Exception:
            logger.error("Failure file check failed", exc_info=True)

        # ── Detect local HEAD changes ────────────────────────────────
        try:
            current_head = await self._get_head()
        except Exception:
            logger.error("Failed to get Genesis HEAD", exc_info=True)
            return SignalReading(
                name=self.signal_name, value=0.0,
                source="genesis_version", collected_at=now_iso, failed=True,
            )

        last_known = await self._get_baseline()
        if last_known is None:
            # First run — store baseline
            try:
                await self._store_baseline(current_head)
            except Exception:
                logger.error("Failed to store Genesis version baseline", exc_info=True)
            return SignalReading(
                name=self.signal_name, value=0.0,
                source="genesis_version", collected_at=now_iso,
            )

        if current_head != last_known:
            # Local version changed (update was applied) — record the
            # change AND resolve any prior unresolved update_available
            # observations so the dashboard alert clears.
            try:
                await self._store_version_change(last_known, current_head)
                await self._resolve_pending_update_available(current_head)
            except Exception:
                logger.error(
                    "Failed to store Genesis version change %s -> %s",
                    last_known, current_head, exc_info=True,
                )
            logger.info("Genesis version changed: %s -> %s", last_known, current_head)
            return SignalReading(
                name=self.signal_name, value=1.0,
                source="genesis_version", collected_at=now_iso,
            )

        # ── Self-throttled remote check ──────────────────────────────
        interval_hours = config.get("check", {}).get("interval_hours", 6)
        should_fetch = (
            self._last_fetch_at is None
            or (now - self._last_fetch_at).total_seconds() >= interval_hours * 3600
        )

        if should_fetch:
            try:
                behind, summary = await self._check_upstream()
                self._last_fetch_at = now

                if behind > 0:
                    stored = await self._store_update_available(
                        current_head, behind, summary,
                    )
                    if stored:
                        await self._notify_update_available(config, behind, summary)
                    return SignalReading(
                        name=self.signal_name, value=1.0,
                        source="genesis_version", collected_at=now_iso,
                    )
            except Exception:
                logger.error("Upstream check failed", exc_info=True)
                self._last_fetch_at = now  # Don't retry immediately on failure

        return SignalReading(
            name=self.signal_name, value=0.0,
            source="genesis_version", collected_at=now_iso,
        )

    # ── Git operations ────────────────────────────────────────────────

    async def _get_head(self) -> str:
        """Get current local HEAD short hash."""
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(f"git rev-parse failed: {stderr.decode(errors='replace')}")
        return stdout.decode().strip()

    async def _check_upstream(self) -> tuple[int, str]:
        """Fetch origin/main and return (commits_behind, summary).

        Returns (0, "") only when origin/main is reachable AND we are
        up to date. Raises RuntimeError on git failure (network down,
        auth failure, missing remote) so the caller can log the error
        properly — silent failures hide broken state.
        """
        # Fetch (updates remote refs, doesn't change working tree)
        proc = await asyncio.create_subprocess_exec(
            "git", "fetch", "origin", "main",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"git fetch origin main failed (exit {proc.returncode}): {stderr_text}"
            )

        # Count commits behind
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-list", "--count", "HEAD..origin/main",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"git rev-list failed (exit {proc.returncode}): {stderr_text}"
            )
        behind = int(stdout.decode().strip())

        if behind == 0:
            return 0, ""

        # Get summary of what's new
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", "HEAD..origin/main",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        summary = stdout.decode().strip() if proc.returncode == 0 else ""
        # Truncate to first 10 lines
        lines = summary.split("\n")
        if len(lines) > 10:
            summary = "\n".join(lines[:10]) + f"\n... and {len(lines) - 10} more"

        return behind, summary

    # ── Observation storage ───────────────────────────────────────────

    async def _get_baseline(self) -> str | None:
        """Read last known Genesis HEAD from observations."""
        cursor = await self._db.execute(
            "SELECT content FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_version_baseline' "
            "ORDER BY created_at DESC LIMIT 1",
        )
        row = await cursor.fetchone()
        if row:
            try:
                data = json.loads(row[0] if isinstance(row, tuple) else row["content"])
                return data.get("version")
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    async def _store_baseline(self, version: str) -> None:
        """Store current HEAD as baseline."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "DELETE FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_version_baseline'",
        )
        await self._db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "genesis_version",
                "genesis_version_baseline",
                json.dumps({"version": version}),
                "low",
                now,
            ),
        )
        await self._db.commit()

    async def _store_version_change(self, old: str, new: str) -> None:
        """Store local version change and update baseline."""
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "genesis_version",
                "genesis_version_change",
                json.dumps({"old_version": old, "new_version": new, "detected_at": now}),
                "medium",
                now,
            ),
        )
        await self._db.commit()
        await self._store_baseline(new)

    async def _resolve_pending_update_available(self, current_head: str) -> None:
        """Resolve any unresolved genesis_update_available observations.

        Called when the local HEAD changes (an update was applied).
        Marks all pending update-available observations as resolved
        with a note pointing to the current head, so the dashboard
        alert clears immediately instead of waiting for the next
        upstream check.
        """
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "UPDATE observations "
            "SET resolved = 1, resolved_at = ?, "
            "    resolution_notes = ? "
            "WHERE source = 'genesis_version' "
            "  AND type = 'genesis_update_available' "
            "  AND resolved = 0",
            (now, f"resolved by local update to {current_head}"),
        )
        await self._db.commit()

    async def _store_update_available(
        self, current: str, behind: int, summary: str,
    ) -> bool:
        """Store update-available observation. Returns True if new (not deduped)."""
        # Get target commit for dedup
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "origin/main",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        target = stdout.decode().strip() if proc.returncode == 0 else "unknown"

        # Dedup: skip if we already have an unresolved observation for this target
        cursor = await self._db.execute(
            "SELECT 1 FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_update_available' "
            "AND resolved = 0 "
            "AND json_extract(content, '$.target_commit') = ? LIMIT 1",
            (target,),
        )
        if await cursor.fetchone():
            return False

        now = datetime.now(UTC).isoformat()

        # Get target tag if available
        proc = await asyncio.create_subprocess_exec(
            "git", "describe", "--tags", "--abbrev=0", "origin/main",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        target_tag = stdout.decode().strip() if proc.returncode == 0 else "untagged"

        await self._db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "genesis_version",
                "genesis_update_available",
                json.dumps({
                    "current_commit": current,
                    "target_commit": target,
                    "target_tag": target_tag,
                    "commits_behind": behind,
                    "summary": summary,
                    "detected_at": now,
                }),
                "medium",
                now,
            ),
        )
        await self._db.commit()
        logger.info(
            "Genesis update available: %d commits behind origin/main (%s)",
            behind, target_tag,
        )
        return True

    # ── Notification ──────────────────────────────────────────────────

    async def _notify_update_available(
        self, config: dict, behind: int, summary: str,
    ) -> None:
        """Send outreach alert about available update. Best-effort."""
        if not config.get("notify", {}).get("enabled", True):
            return

        pipeline = self._pipeline_getter() if callable(self._pipeline_getter) else None
        if pipeline is None:
            return

        try:
            from genesis.outreach.types import OutreachCategory, OutreachRequest

            message = (
                f"New Genesis version available ({behind} commit(s) behind).\n\n"
                f"Highlights:\n{summary}\n\n"
                f"Update from the dashboard when convenient."
            )

            await pipeline.submit(OutreachRequest(
                category=OutreachCategory.DIGEST,
                topic="Genesis update available",
                context=message,
                salience_score=0.4,
                signal_type="genesis_update",
            ))
        except Exception:
            logger.error("Failed to send update notification", exc_info=True)

    # ── Failure file detection ────────────────────────────────────────

    async def _check_failure_file(self) -> None:
        """Check for update failure context from update.sh rollback.

        After successfully storing the failure observation and sending
        the alert, the file is archived to
        ``~/.genesis/update-failures/last_update_failure.<ts>.json``
        so it isn't re-read on every awareness tick AND evidence from
        prior failures isn't overwritten. The archive directory is
        pruned to the most recent ``_FAILURE_ARCHIVE_CAP`` entries on
        each archive call.
        """
        # Always prune first — this catches orphaned archives even on
        # ticks where no new failure is being processed (M4 fix).
        self._prune_failure_archive()

        if not _FAILURE_FILE.exists():
            return

        try:
            data = json.loads(_FAILURE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.error("Failed to parse update failure file", exc_info=True)
            return

        # Dedup: check if we already have an observation for this failure
        ts = data.get("timestamp", "")
        cursor = await self._db.execute(
            "SELECT 1 FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_update_failed' "
            "AND json_extract(content, '$.timestamp') = ? LIMIT 1",
            (ts,),
        )
        if await cursor.fetchone():
            # Already processed — move file out of the way
            self._archive_failure_file()
            return

        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "genesis_version",
                "genesis_update_failed",
                json.dumps(data),
                "high",
                now,
            ),
        )
        await self._db.commit()
        logger.error(
            "Detected update failure: %s -> %s (rolled back to %s)",
            data.get("old_tag"), data.get("new_tag"), data.get("rollback_tag"),
        )

        # Send alert
        pipeline = self._pipeline_getter() if callable(self._pipeline_getter) else None
        if pipeline is not None:
            try:
                from genesis.outreach.types import OutreachCategory, OutreachRequest

                await pipeline.submit(OutreachRequest(
                    category=OutreachCategory.ALERT,
                    topic="Genesis update failed — rolled back",
                    context=(
                        f"Update from {data.get('old_tag')} to {data.get('new_tag')} "
                        f"failed and was rolled back to {data.get('rollback_tag')}.\n"
                        f"Degraded subsystems: {data.get('degraded_subsystems', 'unknown')}\n\n"
                        f"Run Claude Code to diagnose."
                    ),
                    salience_score=0.9,
                    signal_type="genesis_update_failed",
                ))
            except Exception:
                logger.error("Failed to send update failure alert", exc_info=True)

        # Archive the file so we don't re-read it on the next tick
        self._archive_failure_file()

    @staticmethod
    def _archive_failure_file() -> None:
        """Archive the failure file with a timestamp suffix, then prune.

        Moves ``~/.genesis/last_update_failure.json`` to
        ``~/.genesis/update-failures/last_update_failure.<ISO-ts>.json``
        so repeated failures don't overwrite each other (M3 fix), then
        prunes the archive directory to the most recent
        ``_FAILURE_ARCHIVE_CAP`` entries (M4 fix — bounded growth).
        """
        archived = False
        try:
            if not _FAILURE_FILE.exists():
                return

            _FAILURE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            archive_path = _FAILURE_ARCHIVE_DIR / f"last_update_failure.{ts}.json"

            # Same-second collision fallback with an explicit cap so a
            # wedged loop cannot spin forever (L1). In steady state the
            # prune below keeps the directory at _FAILURE_ARCHIVE_CAP,
            # so the real ceiling is ~10 + cap — 10_000 is a hard fuse.
            if archive_path.exists():
                for counter in range(1, 10_000):
                    candidate = _FAILURE_ARCHIVE_DIR / (
                        f"last_update_failure.{ts}.{counter}.json"
                    )
                    if not candidate.exists():
                        archive_path = candidate
                        break
                else:
                    logger.error(
                        "Could not allocate archive slot for %s after 10000 "
                        "attempts — archive directory may be corrupt",
                        _FAILURE_FILE,
                    )
                    return

            _FAILURE_FILE.replace(archive_path)
            archived = True
        except OSError:
            logger.error(
                "Failed to archive update failure file to %s",
                _FAILURE_ARCHIVE_DIR, exc_info=True,
            )

        # Prune outside the archive try-block so a prune failure surfaces
        # with its own clear error message rather than being reported as
        # an archive failure (L2). _prune_failure_archive() has its own
        # error handling.
        if archived:
            GenesisVersionCollector._prune_failure_archive()

    @staticmethod
    def _prune_failure_archive() -> None:
        """Keep only the most recent _FAILURE_ARCHIVE_CAP archive entries.

        Only files matching ``last_update_failure.*.json`` are considered
        — the prefix + suffix filter ensures we never delete files a
        future diagnostic feature might write into the same directory
        (review M2).
        """
        try:
            if not _FAILURE_ARCHIVE_DIR.is_dir():
                return
            entries = sorted(
                (
                    p for p in _FAILURE_ARCHIVE_DIR.iterdir()
                    if p.is_file()
                    and p.suffix == ".json"
                    and p.name.startswith("last_update_failure.")
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in entries[_FAILURE_ARCHIVE_CAP:]:
                try:
                    stale.unlink()
                except OSError:
                    logger.error(
                        "Failed to prune archived failure file %s", stale,
                        exc_info=True,
                    )
        except OSError:
            logger.error("Failed to scan failure archive directory", exc_info=True)
