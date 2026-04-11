"""Tests for genesis.mcp.health.manifest — bootstrap_manifest + job_health.

Focus: the error-handling contract the A2 fix introduced, plus the F3
envelope normalization:

- bootstrap_manifest must log at ERROR on standalone-mode fallback.
- job_health must return a single normalized envelope on EVERY path:
  ``{"jobs": {...}, "note": None | str, "source": str}``. Four sources:
  ``runtime``, ``sqlite``, ``missing_db``, ``query_failed``.
- job_health must log at ERROR on sqlite failure, DEBUG on runtime miss.
- Happy path (standalone, real tmp DB with data) must return structured
  jobs under ``result["jobs"]``, with ``source="sqlite"``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite
import pytest

from genesis.mcp.health import manifest as manifest_mod
from genesis.mcp.health.manifest import _impl_bootstrap_manifest, _impl_job_health


async def _init_job_health_table(db_path: Path) -> None:
    """Create a minimal job_health table for the happy-path test."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            CREATE TABLE job_health (
                job_name TEXT PRIMARY KEY,
                last_run TEXT,
                last_success TEXT,
                last_failure TEXT,
                last_error TEXT,
                consecutive_failures INTEGER
            )
            """
        )
        await db.execute(
            "INSERT INTO job_health VALUES (?, ?, ?, ?, ?, ?)",
            (
                "dream_cycle",
                "2026-04-10T12:00:00+00:00",
                "2026-04-10T12:00:30+00:00",
                None,
                None,
                0,
            ),
        )
        await db.commit()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point manifest.py at an empty tmp_path-backed DB."""
    db_path = tmp_path / "genesis.db"
    monkeypatch.setattr(manifest_mod, "_DB_PATH", db_path)
    return db_path


@pytest.fixture()
def no_runtime():
    """Force the GenesisRuntime import to fail, simulating a missing runtime.

    Clears ALL ``genesis.runtime*`` entries from ``sys.modules`` — not just
    the top-level ``genesis.runtime`` — so submodules imported before the
    fixture runs can't leak through. Restores every saved entry on teardown.
    """
    import sys

    to_clear = [
        k for k in list(sys.modules)
        if k == "genesis.runtime" or k.startswith("genesis.runtime.")
    ]
    saved = {k: sys.modules.pop(k) for k in to_clear}

    class _BrokenRuntime:
        def __getattr__(self, name):
            raise ImportError("genesis.runtime forced unavailable for test")

    sys.modules["genesis.runtime"] = _BrokenRuntime()
    try:
        yield
    finally:
        sys.modules.pop("genesis.runtime", None)
        sys.modules.update(saved)


class TestBootstrapManifest:

    @pytest.mark.asyncio
    async def test_standalone_fallback_returns_structured_dict(
        self, no_runtime, caplog,
    ) -> None:
        """Runtime unavailable → structured dict, not silent empty."""
        with caplog.at_level(logging.ERROR, logger="genesis.mcp.health.manifest"):
            result = await _impl_bootstrap_manifest()

        assert isinstance(result, dict)
        assert result["status"] == "unavailable"
        assert "runtime unreachable" in result["message"].lower()

        # Critical: must log at ERROR, not DEBUG.
        error_records = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "bootstrap_manifest fallback fired" in r.message
        ]
        assert len(error_records) == 1, (
            "Expected exactly one ERROR-level record from bootstrap_manifest "
            "standalone fallback; got: "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )


class TestJobHealth:

    @pytest.mark.asyncio
    async def test_missing_db_returns_note_not_empty(self, tmp_db, no_runtime) -> None:
        """Fresh install, no DB file — must surface state, not silent {}."""
        assert not tmp_db.exists()
        result = await _impl_job_health()

        assert isinstance(result, dict)
        assert result["jobs"] == {}
        assert result["note"] is not None
        assert "not found" in result["note"].lower()
        assert result["source"] == "missing_db"

    @pytest.mark.asyncio
    async def test_sqlite_error_returns_note_and_logs_error(
        self, tmp_db, no_runtime, monkeypatch, caplog,
    ) -> None:
        """DB exists but connect raises → note + empty jobs + ERROR log."""
        # Make the file exist so we bypass the missing-DB branch.
        tmp_db.write_bytes(b"")

        # aiosqlite.connect is a plain callable that returns an async
        # context manager. Patch it at the module-under-test's lookup
        # site to raise synchronously before any awaiting happens.
        def _broken_connect(*args, **kwargs):
            raise aiosqlite.OperationalError("simulated DB failure")

        monkeypatch.setattr(
            "genesis.mcp.health.manifest.aiosqlite.connect", _broken_connect,
        )

        with caplog.at_level(logging.ERROR, logger="genesis.mcp.health.manifest"):
            result = await _impl_job_health()

        assert isinstance(result, dict)
        assert result["jobs"] == {}
        assert result["note"] is not None
        assert "failed" in result["note"].lower()
        assert result["source"] == "query_failed"

        error_records = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "job_health sqlite fallback failed" in r.message
        ]
        assert len(error_records) == 1, (
            f"Expected one ERROR record for sqlite failure; got: "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_os_error_during_connect_returns_note(
        self, tmp_db, no_runtime, monkeypatch,
    ) -> None:
        """OSError during connect (e.g. permission denied) → note, not crash."""
        tmp_db.write_bytes(b"")

        def _permission_denied(*args, **kwargs):
            raise PermissionError("simulated permission denied")

        monkeypatch.setattr(
            "genesis.mcp.health.manifest.aiosqlite.connect", _permission_denied,
        )

        result = await _impl_job_health()
        assert result["jobs"] == {}
        assert "failed" in result["note"].lower()
        assert result["source"] == "query_failed"

    @pytest.mark.asyncio
    async def test_happy_path_standalone_with_real_db(
        self, tmp_db, no_runtime,
    ) -> None:
        """Real sqlite DB with one row → normalized envelope with jobs."""
        await _init_job_health_table(tmp_db)

        result = await _impl_job_health()

        assert isinstance(result, dict)
        # Post-F3: every return path uses the normalized envelope
        # ``{"jobs": {...}, "note": None | str, "source": str}``. The
        # sqlite happy path carries ``source="sqlite"`` and
        # ``note=None``; job rows live under ``result["jobs"]``.
        assert result["source"] == "sqlite"
        assert result["note"] is None
        assert "dream_cycle" in result["jobs"]
        assert result["jobs"]["dream_cycle"]["last_success"] == "2026-04-10T12:00:30+00:00"
        assert result["jobs"]["dream_cycle"]["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_happy_path_multiple_rows(self, tmp_db, no_runtime) -> None:
        """Multiple rows → each one keyed by job_name with independent fields.

        Guards against a loop bug that overwrites the dict key or only
        consumes the first row.
        """
        await _init_job_health_table(tmp_db)
        async with aiosqlite.connect(str(tmp_db)) as db:
            await db.executemany(
                "INSERT INTO job_health VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        "awareness_tick",
                        "2026-04-10T12:05:00+00:00",
                        "2026-04-10T12:05:02+00:00",
                        None, None, 0,
                    ),
                    (
                        "backup",
                        "2026-04-10T06:00:00+00:00",
                        None,
                        "2026-04-10T06:00:15+00:00",
                        "GitHub push timeout",
                        3,
                    ),
                ],
            )
            await db.commit()

        result = await _impl_job_health()

        assert result["source"] == "sqlite"
        assert result["note"] is None
        assert set(result["jobs"].keys()) == {"dream_cycle", "awareness_tick", "backup"}
        assert result["jobs"]["awareness_tick"]["consecutive_failures"] == 0
        assert result["jobs"]["backup"]["consecutive_failures"] == 3
        assert result["jobs"]["backup"]["last_error"] == "GitHub push timeout"
        # Critical: each row's fields are independent. A loop bug that
        # overwrites the same dict would make all three rows identical.
        assert (
            result["jobs"]["dream_cycle"]["last_success"]
            != result["jobs"]["awareness_tick"]["last_success"]
        )

    @pytest.mark.asyncio
    async def test_db_exists_but_table_missing_returns_failure_note(
        self, tmp_db, no_runtime,
    ) -> None:
        """Fresh DB without the job_health table → failure note.

        This documents current behavior: the SELECT raises
        ``aiosqlite.OperationalError: no such table`` which lands in the
        ``(aiosqlite.Error, OSError)`` branch, producing the generic
        "check failed" note. A future enhancement could add a
        sqlite_master probe (matching update_history.py) to return a
        more specific "table not yet created" note. Test codifies
        current behavior so the contract is explicit.
        """
        # Create the DB file with a placeholder table — no job_health.
        async with aiosqlite.connect(str(tmp_db)) as db:
            await db.execute("CREATE TABLE placeholder (id INTEGER)")
            await db.commit()

        result = await _impl_job_health()

        assert result["jobs"] == {}
        assert "failed" in result["note"].lower()
        assert result["source"] == "query_failed"

    @pytest.mark.asyncio
    async def test_runtime_probe_logs_at_debug_not_error(
        self, tmp_db, no_runtime, caplog,
    ) -> None:
        """Runtime-probe failure is expected in standalone — DEBUG, not ERROR.

        Only the sqlite-fallback failure should log at ERROR. The initial
        runtime probe missing is the normal standalone path and must not
        pollute ERROR logs.
        """
        # Force the sqlite fallback to succeed (missing DB branch).
        assert not tmp_db.exists()

        with caplog.at_level(logging.DEBUG, logger="genesis.mcp.health.manifest"):
            await _impl_job_health()

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records == [], (
            f"Runtime probe should not log at ERROR; got: "
            f"{[(r.levelname, r.message) for r in error_records]}"
        )

        # And the DEBUG-level runtime probe record is present.
        debug_records = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG
            and "Runtime job_health unavailable" in r.message
        ]
        assert len(debug_records) == 1
