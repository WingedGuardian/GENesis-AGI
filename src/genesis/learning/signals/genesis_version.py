"""GenesisVersionCollector — detects Genesis repo updates available upstream.

Checks local HEAD against the resolved deploy branch on a self-throttled
interval (default 6h, configurable via config/updates.yaml). When upstream has
new commits, stores a genesis_update_available observation and optionally
sends a Telegram notification via the outreach pipeline.

Also detects local version changes (e.g., after an update was applied)
and checks for update failure context files left by update.sh.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import yaml

from genesis.awareness.types import SignalReading
from genesis.env import deploy_target, update_in_progress

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
_UPDATE_CHECK_REF = "refs/genesis-update-check"


def _load_updates_config() -> dict:
    """Load config/updates.yaml. Returns defaults if missing."""
    path = _CONFIG_DIR / "updates.yaml"
    if path.is_file():
        from genesis._config_overlay import merge_local_overlay

        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        return merge_local_overlay(raw, path)
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
                await self._resolve_pending_update_failed(current_head)
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
                behind, summary, target_commit = await self._check_upstream()
                self._last_fetch_at = now

                if behind == 0:
                    # A MEASURED zero must clear a stale alert. Before this
                    # method stopped coercing with max(behind, 1), zero was
                    # unreachable here, so nothing downstream was written for
                    # it. Now that a genuine zero can arrive, an upstream ref
                    # that was rewritten or rolled back after an observation
                    # was stored would otherwise leave the dashboard claiming
                    # an update forever: HEAD never changes, so the resolve on
                    # the HEAD-change path never fires, and update_status keeps
                    # serving the unresolved target.
                    await self._resolve_pending_update_available(current_head)

                if behind > 0:
                    stored = await self._store_update_available(
                        current_head, behind, summary, target_commit,
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
                # A FAILED check is not "up to date". Falling through to the
                # 0.0 return below would report an unknown state as the
                # explicit baseline "0.0=up to date" with failed=False — the
                # same defect class this method was rewritten to remove, since
                # it is false about the reader while wearing verified grammar.
                # Same shape as the HEAD-read failure above.
                return SignalReading(
                    name=self.signal_name,
                    value=0.0,
                    source="genesis_version",
                    collected_at=now_iso,
                    failed=True,
                )

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

    async def _delete_ref(self, ref: str) -> None:
        """Remove a private per-check ref. Never raises; cleanup is not the job."""
        cleanup = await asyncio.create_subprocess_exec(
            "git", "update-ref", "-d", ref,
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(cleanup.communicate(), timeout=10)

    async def _best_effort_fetch(self, *args: str, what: str) -> None:
        """Refresh something convenient. A failure is logged, never raised.

        Kept separate from the deploy-head fetch on purpose: a tag that was
        rewritten upstream, or a remote-tracking ref blocked by an obsolete
        ancestor, says nothing about whether the deploy head was fetched, and
        treating it as fatal made this collector report a failed check every six
        hours on a repository that was perfectly reachable.
        """
        proc = await asyncio.create_subprocess_exec(
            "git", "fetch", *args,
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            logger.warning("Refreshing %s timed out (non-fatal)", what)
            return
        if proc.returncode != 0:
            logger.warning(
                "Could not refresh %s (non-fatal, deploy head unaffected): %s",
                what,
                stderr.decode(errors="replace").strip(),
            )

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

    async def _check_upstream(self) -> tuple[int, str, str]:
        """Fetch upstream and measure the deployed commit's own distance.

        Uses the remote pointing to github_public_repo() (e.g. 'public'),
        falling back to 'origin'. The fetched commit itself is the target:
        release tags remain display metadata elsewhere, but a matching nearest
        tag cannot suppress post-tag commits.

        Returns (0, "", target_commit) when up to date, otherwise
        (N, summary, target_commit) where N is how far the DEPLOYED COMMIT is
        behind the fetched target and summary lists that same range. Not the
        distance between the release tags — the caller renders this as "N commits
        behind", which a reader takes as their own, and on an install that
        tracks the deploy branch between releases those numbers differ by an
        order of magnitude.
        Raises RuntimeError on git failure.
        """
        remote, deploy_branch = await asyncio.to_thread(
            deploy_target, _GENESIS_ROOT
        )
        ref = f"{_UPDATE_CHECK_REF}/collector/{uuid.uuid4().hex}"
        tracking_ref = f"refs/remotes/{remote}/{deploy_branch}"

        # Fetch the same deploy target update.sh uses. The per-check ref is a
        # one-shot name for the fetched commit; resolve the SHA immediately so a
        # concurrent collector/dashboard check cannot move this measurement's
        # target, then remove the private ref.
        # ONE fatal fetch: the private per-check ref, which is the only thing
        # this measurement needs. The tracking ref and the tags are refreshed
        # separately and best-effort below, because both fail for reasons that
        # say nothing about whether the deploy head was fetched -- and bundled
        # here, either one turned a good measurement into a failed check every
        # six hours. A rewritten upstream tag makes `--tags` exit non-zero with
        # "would clobber existing tag", and after a default-branch hierarchy
        # change an existing refs/remotes/<remote>/release blocks
        # refs/remotes/<remote>/release/v2. The dashboard already split the tags
        # out for exactly this reason; this path had not.
        proc = await asyncio.create_subprocess_exec(
            "git", "fetch", remote,
            f"+refs/heads/{deploy_branch}:{ref}",
            cwd=str(_GENESIS_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            # Delete the private ref before raising. A partially successful
            # fetch creates it and still exits non-zero, and the cleanup below
            # only runs once this point is passed, so raising here used to leak
            # one uniquely-named ref per failed check.
            await self._delete_ref(ref)
            stderr_text = stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"git fetch {remote} {deploy_branch} failed (exit {proc.returncode}): {stderr_text}"
            )

        await self._best_effort_fetch(
            remote, f"+refs/heads/{deploy_branch}:{tracking_ref}", what=tracking_ref
        )
        # `--force` because a rewritten tag is the case that made this fatal.
        await self._best_effort_fetch(remote, "--tags", "--force", what="tags")

        try:
            target_out = await self._git_output("rev-parse", "--verify", f"{ref}^{{commit}}")
        finally:
            await self._delete_ref(ref)
        if target_out is None:
            raise RuntimeError(
                f"git fetch {remote} {deploy_branch} did not leave a measurable head"
            )
        target_commit = target_out

        # Matching nearest tags prove only a shared release ancestor; the
        # fetched deploy head can still contain commits after that tag. Measure
        # the deployed commit's own distance every time rather than turning a
        # matching tag into a false all-clear.
        behind, summary = await self._check_upstream_by_commits(target_commit)
        return behind, summary, target_commit

    async def _check_upstream_by_commits(self, ref: str) -> tuple[int, str]:
        """Count commits from the deployed commit to a fetched target."""
        count_str = await self._git_output("rev-list", "--count", f"HEAD..{ref}")
        if count_str is None or not count_str.isdigit():
            raise RuntimeError(
                f"git rev-list --count HEAD..{ref} failed; distance unknown"
            )
        behind = int(count_str)

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
        from genesis.db.crud import observations

        rows = await observations.query(
            self._db,
            source="genesis_version",
            type="genesis_version_baseline",
            limit=1,
        )
        if rows:
            try:
                data = json.loads(rows[0].get("content", "{}"))
                return data.get("version")
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    async def _store_baseline(self, version: str) -> None:
        """Store current HEAD as baseline."""
        from genesis.db.crud import observations

        now = datetime.now(UTC).isoformat()
        await observations.delete_by_source_and_type(
            self._db, source="genesis_version", type="genesis_version_baseline",
        )
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

        TWO callers, and the second is not an update being applied:
        (1) the local HEAD changed — an update was applied;
        (2) an upstream check MEASURED zero commits behind — the target went
            away (ref rewritten or rolled back) while HEAD stayed put, so no
            HEAD change will ever fire and the alert would otherwise never
            clear.
        Marks all pending update-available observations as resolved
        with a note pointing to the current head, so the dashboard
        alert clears immediately instead of waiting for the next
        upstream check.
        """
        from genesis.db.crud import observations

        now = datetime.now(UTC).isoformat()
        await observations.resolve_by_source_and_type(
            self._db,
            source="genesis_version",
            type="genesis_update_available",
            resolved_at=now,
            resolution_notes=f"resolved by local update to {current_head}",
        )

    async def _resolve_pending_update_failed(self, current_head: str) -> None:
        """Resolve any unresolved genesis_update_failed observations.

        Called when the local HEAD changes (an update was applied).
        If Genesis successfully updates past a previously failed version,
        the failure observation is no longer relevant — resolve it so the
        alert clears and the sentinel can auto-unstick.
        """
        from genesis.db.crud import observations

        now = datetime.now(UTC).isoformat()
        await observations.resolve_by_source_and_type(
            self._db,
            source="genesis_version",
            type="genesis_update_failed",
            resolved_at=now,
            resolution_notes=f"resolved by successful update to {current_head}",
        )

    async def _store_update_available(
        self, current: str, behind: int, summary: str, target_commit: str,
    ) -> bool:
        """Store update-available observation. Returns True if new (not deduped)."""
        target = target_commit

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
            "git", "describe", "--tags", "--match", "v*", "--abbrev=0", target_commit,
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
            "Genesis update available: %d commits behind deploy target (%s)",
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
                verbatim=True,  # pre-composed factual notice — no LLM rewrite
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

        # Gate: if a deploy is still in progress — a dashboard-orchestrated
        # multi-tier run OR a CLI `update.sh` run — defer the alarm. A later tier
        # may succeed, and a failure file written mid-deploy is expected transient
        # noise, not a settled failure. (Shared with the watchdog's restart guard.)
        if update_in_progress():
            logger.debug(
                "Update failure file present but a deploy is still in progress "
                "— deferring observation",
            )
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
                    verbatim=True,  # machine fact (rollback tags) — never reword
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
