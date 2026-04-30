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
import os
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
        from genesis._config_overlay import merge_local_overlay

        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        return merge_local_overlay(raw, path)
    return {"check": {"enabled": True, "interval_hours": 6}}


def _update_remote() -> str:
    """Return the git remote that points to the public/primary repo.

    Reads github.public_repo from genesis.env and matches it against
    'git remote -v'. Falls back to 'origin' if detection fails.
    """
    import subprocess
    try:
        from genesis.env import github_public_repo
        public_repo = github_public_repo()
        result = subprocess.run(
            ["git", "-C", str(_GENESIS_ROOT), "remote", "-v"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if public_repo in line and "(fetch)" in line:
                    return line.split()[0]
    except Exception:
        pass
    return "origin"


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
                baseline_note="1.0=Genesis version just changed or upstream update available",
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
                        baseline_note="1.0=upstream update available",
                    )
            except Exception:
                logger.error("Upstream check failed", exc_info=True)
                self._last_fetch_at = now  # Don't retry immediately on failure

        return SignalReading(
            name=self.signal_name, value=0.0,
            source="genesis_version", collected_at=now_iso,
            baseline_note="0.0=up to date (normal). 1.0=update available or just applied",
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

    async def _git_output(self, *args: str, timeout: int = 10) -> str | None:
        """Run a git command and return stdout, or None on failure."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return None
        return stdout.decode().strip()

    async def _check_upstream(self) -> tuple[int, str]:
        """Fetch upstream and compare release tags.

        Uses the remote pointing to github_public_repo() (e.g. 'public'),
        falling back to 'origin'. Tag-based comparison is robust against
        squash-merge divergence — same release tag means same content even
        if commit SHAs differ.

        Returns (0, "") when tags match (up to date).
        Returns (N, summary) where N is commits between tags and summary
        shows what changed in the tag range.
        Raises RuntimeError on git failure.
        """
        remote = _update_remote()
        ref = f"{remote}/main"

        # Fetch (updates remote refs + tags, doesn't change working tree)
        proc = await asyncio.create_subprocess_exec(
            "git", "fetch", remote, "main", "--tags",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"git fetch {remote} main failed (exit {proc.returncode}): {stderr_text}"
            )

        # Get local and remote release tags
        local_tag = await self._git_output(
            "describe", "--tags", "--match", "v*", "--abbrev=0", "HEAD",
        )
        origin_tag = await self._git_output(
            "describe", "--tags", "--match", "v*", "--abbrev=0", ref,
        )

        # If neither side has tags, fall back to commit-based comparison
        if not local_tag and not origin_tag:
            return await self._check_upstream_by_commits()

        # If only one side has tags, there's definitely an update
        if local_tag != origin_tag:
            # Count commits in the tag range for a meaningful number
            behind = 0
            if local_tag and origin_tag:
                count_str = await self._git_output(
                    "rev-list", "--count", f"{local_tag}..{origin_tag}",
                )
                behind = int(count_str) if count_str and count_str.isdigit() else 1
            else:
                # One side untagged — use commit count as fallback
                count_str = await self._git_output(
                    "rev-list", "--count", f"HEAD..{ref}",
                )
                behind = int(count_str) if count_str and count_str.isdigit() else 1

            # Summary of what changed between tags
            summary = ""
            if local_tag and origin_tag:
                raw = await self._git_output(
                    "log", "--oneline", "--no-merges",
                    f"{local_tag}..{origin_tag}",
                )
                summary = raw or ""
            else:
                raw = await self._git_output(
                    "log", "--oneline", "--no-merges", f"HEAD..{ref}",
                )
                summary = raw or ""

            # Truncate to first 10 lines
            lines = summary.split("\n")
            if len(lines) > 10:
                summary = "\n".join(lines[:10]) + f"\n... and {len(lines) - 10} more"

            return max(behind, 1), summary

        # Same tag — up to date
        return 0, ""

    async def _check_upstream_by_commits(self) -> tuple[int, str]:
        """Fallback: count commits when no release tags exist."""
        ref = f"{_update_remote()}/main"
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-list", "--count", f"HEAD..{ref}",
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

        raw = await self._git_output(
            "log", "--oneline", "--no-merges", f"HEAD..{ref}",
        )
        summary = raw or ""
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
        from genesis.db.crud import observations

        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "DELETE FROM observations "
            "WHERE source = 'genesis_version' AND type = 'genesis_version_baseline'",
        )
        await self._db.commit()
        await observations.create(
            self._db,
            id=str(uuid.uuid4()),
            source="genesis_version",
            type="genesis_version_baseline",
            content=json.dumps({"version": version}),
            priority="low",
            created_at=now,
        )

    async def _store_version_change(self, old: str, new: str) -> None:
        """Store local version change and update baseline."""
        from genesis.db.crud import observations

        now = datetime.now(UTC).isoformat()
        await observations.create(
            self._db,
            id=str(uuid.uuid4()),
            source="genesis_version",
            type="genesis_version_change",
            content=json.dumps({"old_version": old, "new_version": new, "detected_at": now}),
            priority="medium",
            created_at=now,
        )
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
        ref = f"{_update_remote()}/main"
        # Get target commit for dedup
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", ref,
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

        from genesis.db.crud import observations as obs_crud

        now = datetime.now(UTC).isoformat()

        # Get target tag if available
        proc = await asyncio.create_subprocess_exec(
            "git", "describe", "--tags", "--match", "v*", "--abbrev=0", ref,
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        target_tag = stdout.decode().strip() if proc.returncode == 0 else "untagged"

        await obs_crud.create(
            self._db,
            id=str(uuid.uuid4()),
            source="genesis_version",
            type="genesis_update_available",
            content=json.dumps({
                "current_commit": current,
                "target_commit": target,
                "target_tag": target_tag,
                "commits_behind": behind,
                "summary": summary,
                "detected_at": now,
            }),
            priority="medium",
            created_at=now,
        )
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

        # Gate: if the update orchestrator is still running (multi-tier),
        # defer the alarm. Tier 1 may fail and write this file, but Tier 2
        # may succeed — don't fire a false alarm mid-orchestration.
        _ORCHESTRATOR_PID_FILE = Path.home() / ".genesis" / "update_in_progress.pid"
        if _ORCHESTRATOR_PID_FILE.exists():
            try:
                pid = int(_ORCHESTRATOR_PID_FILE.read_text().strip())
                if pid > 1:
                    os.kill(pid, 0)  # Check if alive (signal 0 = existence check)
                    logger.debug(
                        "Update failure file present but orchestrator still running "
                        "(pid=%d) — deferring observation", pid,
                    )
                    return
            except (ProcessLookupError, ValueError, OSError):
                pass  # PID dead or invalid — orchestrator finished, proceed

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

        from genesis.db.crud import observations as obs_crud

        now = datetime.now(UTC).isoformat()
        await obs_crud.create(
            self._db,
            id=str(uuid.uuid4()),
            source="genesis_version",
            type="genesis_update_failed",
            content=json.dumps(data),
            priority="high",
            created_at=now,
        )
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
