"""The migration CLI must honour GENESIS_DB_PATH.

Without this, `GENESIS_DB_PATH=~/tmp/copy.db python -m genesis.db.migrations
--apply` — the obvious way to test a migration against a copy — silently
rewrites the PRODUCTION database instead (it happened, 2026-07-23). The fallback
must stay HOME-anchored, not repo_root()/data, so a run from a worktree checkout
still targets the real production DB (feedback_hook_prod_db_home_anchored).
"""

from __future__ import annotations

from pathlib import Path

from genesis.db.migrations.__main__ import _resolve_db_path


class TestResolveDbPath:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        target = tmp_path / "copy.db"
        monkeypatch.setenv("GENESIS_DB_PATH", str(target))
        assert _resolve_db_path() == target

    def test_env_override_expands_user(self, monkeypatch):
        monkeypatch.setenv("GENESIS_DB_PATH", "~/tmp/copy.db")
        assert _resolve_db_path() == Path.home() / "tmp" / "copy.db"

    def test_fallback_is_home_anchored(self, monkeypatch):
        """Not repo_root()/data — a worktree run must still hit the real DB."""
        monkeypatch.delenv("GENESIS_DB_PATH", raising=False)
        assert _resolve_db_path() == Path.home() / "genesis" / "data" / "genesis.db"

    def test_fallback_ignores_cwd(self, monkeypatch, tmp_path):
        """CWD (e.g. a worktree) must not change the resolved production path."""
        monkeypatch.delenv("GENESIS_DB_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        assert _resolve_db_path() == Path.home() / "genesis" / "data" / "genesis.db"
