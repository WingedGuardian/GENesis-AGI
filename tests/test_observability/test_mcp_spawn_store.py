"""Tests for the per-slot spawn-identity file plane (Part B cross-process handoff)."""

from __future__ import annotations

import os

from genesis.observability import mcp_spawn_store as store

FULL = "0123456789abcdef0123456789abcdef01234567"
AT = "2026-08-05T12:00:00+00:00"


def _point_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SPAWN_DIR", tmp_path / "mcp-spawn")


class TestPersistRead:
    def test_roundtrip(self, tmp_path, monkeypatch):
        _point_dir(tmp_path, monkeypatch)
        store.persist_spawn_commit("4", 45285, FULL, AT)
        assert store.read_spawn_identity("4", 45285) == (FULL, AT)

    def test_pid_mismatch_reads_none(self, tmp_path, monkeypatch):
        # Slot reused by a new session (different claude pid) that hasn't
        # rewritten the file yet → old pid in file → fail-open None.
        _point_dir(tmp_path, monkeypatch)
        store.persist_spawn_commit("4", 111, FULL, AT)
        assert store.read_spawn_identity("4", 222) is None

    def test_missing_file_reads_none(self, tmp_path, monkeypatch):
        _point_dir(tmp_path, monkeypatch)
        assert store.read_spawn_identity("9", 100) is None

    def test_malformed_file_reads_none(self, tmp_path, monkeypatch):
        _point_dir(tmp_path, monkeypatch)
        d = tmp_path / "mcp-spawn"
        d.mkdir()
        (d / "4").write_text("only two tokens\n")  # not 3
        assert store.read_spawn_identity("4", 100) is None

    def test_non_numeric_pid_reads_none(self, tmp_path, monkeypatch):
        _point_dir(tmp_path, monkeypatch)
        d = tmp_path / "mcp-spawn"
        d.mkdir()
        (d / "4").write_text(f"notapid {FULL} {AT}\n")
        assert store.read_spawn_identity("4", 100) is None

    def test_overwrite_on_reuse(self, tmp_path, monkeypatch):
        # A new session (new pid, new commit) overwrites the slot file.
        _point_dir(tmp_path, monkeypatch)
        store.persist_spawn_commit("4", 111, FULL, AT)
        store.persist_spawn_commit("4", 222, "abcdef01", "2026-08-06T00:00:00+00:00")
        assert store.read_spawn_identity("4", 111) is None
        assert store.read_spawn_identity("4", 222) == ("abcdef01", "2026-08-06T00:00:00+00:00")

    def test_no_torn_read_leaves_single_file(self, tmp_path, monkeypatch):
        _point_dir(tmp_path, monkeypatch)
        store.persist_spawn_commit("4", 111, FULL, AT)
        entries = list((tmp_path / "mcp-spawn").iterdir())
        assert [e.name for e in entries] == ["4"]

    def test_persist_ignores_missing_inputs(self, tmp_path, monkeypatch):
        _point_dir(tmp_path, monkeypatch)
        store.persist_spawn_commit("", 111, FULL, AT)
        store.persist_spawn_commit("4", None, FULL, AT)
        store.persist_spawn_commit("4", 111, None, AT)
        store.persist_spawn_commit("4", 111, FULL, None)  # missing spawn_at
        assert not (tmp_path / "mcp-spawn").exists() or not list((tmp_path / "mcp-spawn").iterdir())

    def test_persist_never_raises_on_bad_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_SPAWN_DIR", tmp_path / "afile" / "sub")
        (tmp_path / "afile").write_text("not a dir")
        store.persist_spawn_commit("4", 111, FULL, AT)  # must not raise


class TestSessionPid:
    def test_parent_is_claude(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_PROC", str(tmp_path))
        (tmp_path / "5000").mkdir()
        (tmp_path / "5000" / "comm").write_text("claude\n")
        monkeypatch.setattr(os, "getppid", lambda: 5000)
        assert store.session_pid() == 5000

    def test_parent_not_claude_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_PROC", str(tmp_path))
        (tmp_path / "5001").mkdir()
        (tmp_path / "5001" / "comm").write_text("bash\n")
        monkeypatch.setattr(os, "getppid", lambda: 5001)
        assert store.session_pid() is None

    def test_parent_gone_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_PROC", str(tmp_path))
        monkeypatch.setattr(os, "getppid", lambda: 999999)
        assert store.session_pid() is None
