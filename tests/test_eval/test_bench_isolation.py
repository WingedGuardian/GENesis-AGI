"""Tests for the bench isolation layer (snapshot, MCP config, prod probe)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from genesis.eval.bench.isolation import (
    BENCH_MCP_SERVERS,
    ProdDeltaProbe,
    ProdSnapshot,
    count_snapshot_eval_events,
    generate_bench_mcp_config,
    snapshot_prod_db,
)


def _make_db(path, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE eval_events (id INTEGER PRIMARY KEY, x TEXT)")
    conn.executemany(
        "INSERT INTO eval_events (x) VALUES (?)", [(f"r{i}",) for i in range(rows)]
    )
    conn.commit()
    conn.close()


class TestSnapshot:
    def test_backup_copies_rows_and_leaves_source_untouched(self, tmp_path):
        src = tmp_path / "prod.db"
        _make_db(src, rows=5)
        mtime_before = src.stat().st_mtime_ns

        run_dir = tmp_path / "run"
        dest = snapshot_prod_db(run_dir, source=src)

        assert dest == run_dir / "genesis.db"
        assert count_snapshot_eval_events(dest) == 5
        assert src.stat().st_mtime_ns == mtime_before

    def test_snapshot_sees_wal_resident_writes(self, tmp_path):
        """The backup API must include un-checkpointed WAL pages — the reason
        a plain file copy is wrong for the live DB."""
        src = tmp_path / "prod.db"
        conn = sqlite3.connect(src)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE eval_events (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO eval_events DEFAULT VALUES")
        conn.commit()  # committed but not checkpointed — lives in -wal

        dest = snapshot_prod_db(tmp_path / "run", source=src)
        assert count_snapshot_eval_events(dest) == 1
        conn.close()


class TestBenchMcpConfig:
    def test_generates_memory_only_with_env_block(self, tmp_path):
        db_copy = tmp_path / "genesis.db"
        db_copy.touch()
        out = generate_bench_mcp_config(tmp_path, db_copy)

        config = json.loads(out.read_text())
        assert set(config["mcpServers"]) == BENCH_MCP_SERVERS
        server = config["mcpServers"]["genesis-memory"]
        assert server["env"]["GENESIS_DB_PATH"] == str(db_copy)
        assert server["env"]["GENESIS_MEMORY_WRITEBACKS_OFF"] == "1"
        # {{GENESIS_ROOT}} resolved — no template token survives.
        assert "{{GENESIS_ROOT}}" not in out.read_text()
        assert server["command"].endswith(".claude/mcp/run-mcp-server")
        assert server["args"] == ["--server", "memory"]


class TestProdDeltaProbe:
    def _snap(self, **overrides) -> ProdSnapshot:
        snap = ProdSnapshot(
            qdrant_point_counts={"episodic_memory": 100, "knowledge_base": 50},
            qdrant_retrieved_sums={"episodic_memory": 900, "knowledge_base": 40},
            qdrant_last_retrieved_max={
                "episodic_memory": "2026-07-01T00:00:00+00:00",
                "knowledge_base": "2026-06-01T00:00:00+00:00",
            },
            db_row_counts={
                "eval_events": 10, "eval_runs": 2,
                "cc_sessions": 5, "observations": 7,
            },
        )
        for key, val in overrides.items():
            getattr(snap, key).update(val)
        return snap

    def test_clean_when_identical(self, monkeypatch):
        probe = ProdDeltaProbe()
        monkeypatch.setattr(probe, "capture", lambda: self._snap())
        probe.start()
        report = probe.finish()
        assert report["clean"] is True
        assert report["deltas"] == []

    def test_detects_payload_usage_delta_without_count_change(self, monkeypatch):
        """The write-on-read class: retrieved_count bumps change NO point
        counts — the probe must still catch them."""
        probe = ProdDeltaProbe()
        snaps = iter([
            self._snap(),
            self._snap(qdrant_retrieved_sums={"episodic_memory": 903}),
        ])
        monkeypatch.setattr(probe, "capture", lambda: next(snaps))
        probe.start()
        report = probe.finish()
        assert report["clean"] is False
        assert any("sum(retrieved_count) 900 → 903" in d for d in report["deltas"])

    def test_detects_prod_db_row_growth(self, monkeypatch):
        probe = ProdDeltaProbe()
        snaps = iter([
            self._snap(),
            self._snap(db_row_counts={"eval_events": 12}),
        ])
        monkeypatch.setattr(probe, "capture", lambda: next(snaps))
        probe.start()
        report = probe.finish()
        assert report["clean"] is False
        assert any("eval_events rows 10 → 12" in d for d in report["deltas"])

    def test_finish_before_start_raises(self):
        with pytest.raises(RuntimeError, match="before start"):
            ProdDeltaProbe().finish()

    def test_probe_errors_surface_in_report(self, monkeypatch):
        probe = ProdDeltaProbe()
        bad = self._snap()
        bad.errors.append("qdrant episodic_memory: connection refused")
        snaps = iter([bad, self._snap()])
        monkeypatch.setattr(probe, "capture", lambda: next(snaps))
        probe.start()
        report = probe.finish()
        assert "qdrant episodic_memory: connection refused" in report["probe_errors"]
