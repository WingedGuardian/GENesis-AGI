"""CCVersionCollector — detects Claude Code version changes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite

from genesis.awareness.types import SignalReading

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


class CCVersionCollector:
    """Detects CC version changes by comparing against last-known version.

    When a version change is detected:
    - Runs CCUpdateAnalyzer to fetch changelog and classify impact
    - Stores analysis as a recon finding BEFORE returning the signal
    - Emits signal value=1.0 (triggers depth escalation in awareness loop)

    On first run or no change: emits 0.0.
    """

    signal_name = "cc_version_changed"

    def __init__(
        self,
        db: aiosqlite.Connection,
        router: Router | None = None,
        pipeline_getter: object | None = None,
        memory_store_getter: object | None = None,
    ) -> None:
        self._db = db
        self._router = router
        # Accept callables that return dependencies (lazy resolution).
        # This avoids init ordering issues — outreach/memory may not be
        # ready yet when the learning subsystem is constructed.
        self._pipeline_getter = pipeline_getter
        self._memory_store_getter = memory_store_getter

    async def collect(self) -> SignalReading:
        now = datetime.now(UTC).isoformat()
        try:
            current = await self._get_cc_version()
        except Exception:
            logger.warning("Failed to get CC version", exc_info=True)
            return SignalReading(
                name=self.signal_name,
                value=0.0,
                source="cc_version",
                collected_at=now,
                failed=True,
            )

        last_known = await self._get_last_known_version()

        if last_known is None:
            # First run — store current, no change signal
            try:
                await self._store_version(current)
            except Exception:
                logger.warning("Failed to store CC version baseline", exc_info=True)
            return SignalReading(
                name=self.signal_name,
                value=0.0,
                source="cc_version",
                collected_at=now,
            )

        if current != last_known:
            try:
                await self._store_version_change(last_known, current)
                await self._analyze_update(last_known, current)
            except Exception:
                logger.warning(
                    "Failed to store/analyze CC version change %s -> %s",
                    last_known, current, exc_info=True,
                )
            logger.info("CC version changed: %s -> %s", last_known, current)
            return SignalReading(
                name=self.signal_name,
                value=1.0,
                source="cc_version",
                collected_at=now,
            )

        # No local change — check if a newer version is available on npm.
        try:
            await self._check_registry_version(current)
        except Exception:
            logger.debug("Registry version check failed", exc_info=True)

        return SignalReading(
            name=self.signal_name,
            value=0.0,
            source="cc_version",
            collected_at=now,
        )

    async def _get_cc_version(self) -> str:
        """Run `claude --version` via subprocess and extract version string.

        Uses create_subprocess_exec (not shell) with a 15s timeout covering
        both process creation and communication.
        """
        async def _run() -> str:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                stderr_text = stderr.decode(errors="replace").strip()
                logger.warning("claude --version exit %d: %s", proc.returncode, stderr_text)
            return stdout.decode().strip()

        return await asyncio.wait_for(_run(), timeout=15)

    async def _get_last_known_version(self) -> str | None:
        """Read last known CC version from observations table."""
        cursor = await self._db.execute(
            "SELECT content FROM observations "
            "WHERE source = 'cc_version' AND type = 'cc_version_baseline' "
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

    async def _store_version(self, version: str) -> None:
        """Store current version as baseline in observations table."""
        now = datetime.now(UTC).isoformat()
        # Remove previous baselines to keep exactly one current
        await self._db.execute(
            "DELETE FROM observations "
            "WHERE source = 'cc_version' AND type = 'cc_version_baseline'",
        )
        await self._db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "cc_version",
                "cc_version_baseline",
                json.dumps({"version": version}),
                "low",
                now,
            ),
        )
        await self._db.commit()

    async def _store_version_change(self, old: str, new: str) -> None:
        """Store version change observation and update current."""
        now = datetime.now(UTC).isoformat()
        # Store change event
        await self._db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "cc_version",
                "version_change",
                json.dumps({"old_version": old, "new_version": new, "detected_at": now}),
                "medium",
                now,
            ),
        )
        await self._db.commit()
        # Update baseline so next collect() sees the new version
        await self._store_version(new)

    # ------------------------------------------------------------------
    # Remote registry check — detect available versions without installing
    # ------------------------------------------------------------------

    async def _check_registry_version(self, installed: str) -> None:
        """Check npm registry for newer CC version. Best-effort, never blocks.

        Stores a ``cc_version_available`` observation when a newer version
        exists on the registry but is not installed locally.  Deduplicates
        by version so the same available version is only recorded once.
        """
        try:
            available = await self._get_registry_version()
        except Exception:
            logger.debug("npm registry check failed", exc_info=True)
            return

        if not available or available == installed:
            return

        if not self._is_newer(available, installed):
            return

        # Dedup: skip if we already have an observation for this version.
        cursor = await self._db.execute(
            "SELECT 1 FROM observations "
            "WHERE source = 'cc_version' AND type = 'cc_version_available' "
            "AND json_extract(content, '$.available_version') = ? LIMIT 1",
            (available,),
        )
        if await cursor.fetchone():
            return

        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                "cc_version",
                "cc_version_available",
                json.dumps({
                    "installed_version": installed,
                    "available_version": available,
                    "detected_at": now,
                }),
                "medium",
                now,
            ),
        )
        await self._db.commit()
        logger.info(
            "CC version %s available on npm (installed: %s)", available, installed,
        )

    async def _get_registry_version(self) -> str:
        """Query npm registry for latest CC version. 10s timeout."""
        async def _run() -> str:
            proc = await asyncio.create_subprocess_exec(
                "npm", "view", "@anthropic-ai/claude-code", "version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                stderr_text = stderr.decode(errors="replace").strip()
                logger.debug("npm view exit %d: %s", proc.returncode, stderr_text)
                return ""
            return stdout.decode().strip()

        return await asyncio.wait_for(_run(), timeout=10)

    @staticmethod
    def _is_newer(candidate: str, baseline: str) -> bool:
        """Return True if *candidate* is a newer semver than *baseline*."""
        def _parse(v: str) -> tuple[int, ...]:
            m = re.match(r"[\d.]+", v)
            return tuple(int(x) for x in m.group().split(".")) if m else ()

        return _parse(candidate) > _parse(baseline)

    async def _analyze_update(self, old: str, new: str) -> None:
        """Run CCUpdateAnalyzer to fetch changelog and classify impact.

        Best-effort: if analysis fails or times out, the version change
        observation is already stored and the signal still fires.
        """
        try:
            from genesis.recon.cc_update_analyzer import CCUpdateAnalyzer

            pipeline = self._pipeline_getter() if callable(self._pipeline_getter) else None
            memory_store = self._memory_store_getter() if callable(self._memory_store_getter) else None
            analyzer = CCUpdateAnalyzer(
                db=self._db, router=self._router,
                pipeline=pipeline, memory_store=memory_store,
            )
            result = await asyncio.wait_for(analyzer.analyze(old, new), timeout=30)
            logger.info(
                "CC update analysis complete: %s → %s [%s]",
                old, new, result.get("impact", "unknown"),
            )
        except TimeoutError:
            logger.warning("CC update analysis timed out for %s → %s", old, new)
        except Exception:
            logger.warning("CC update analysis failed", exc_info=True)
