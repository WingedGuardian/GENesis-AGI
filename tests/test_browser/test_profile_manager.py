"""Tests for BrowserProfileManager."""

from __future__ import annotations

import json
import sqlite3

import pytest

from genesis.browser.profile import BrowserProfileManager
from genesis.browser.types import BrowserLayer


@pytest.fixture
def profile_dir(tmp_path):
    """Create a temporary profile directory."""
    return tmp_path / "browser-profile"


@pytest.fixture
def manager(profile_dir):
    return BrowserProfileManager(profile_dir)


@pytest.fixture
def populated_profile(profile_dir):
    """Create a profile directory with a mock Chrome cookies database."""
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True)

    cookies_db = default_dir / "Cookies"
    conn = sqlite3.connect(str(cookies_db))
    conn.execute(
        "CREATE TABLE cookies ("
        "host_key TEXT, name TEXT, value TEXT, path TEXT, "
        "is_secure INTEGER, is_httponly INTEGER)"
    )
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?)",
        [
            (".github.com", "session", "abc123", "/", 1, 1),
            (".github.com", "user", "genesis-bot", "/", 1, 0),
            (".google.com", "SID", "xyz789", "/", 1, 1),
            (".google.com", "HSID", "def456", "/", 1, 1),
            (".example.com", "token", "tok_123", "/", 0, 0),
        ],
    )
    conn.commit()
    conn.close()

    return BrowserProfileManager(profile_dir)


class TestEnsureDir:
    def test_creates_directory(self, manager, profile_dir):
        assert not profile_dir.exists()
        result = manager.ensure_dir()
        assert result == profile_dir
        assert profile_dir.exists()

    def test_idempotent(self, manager, profile_dir):
        manager.ensure_dir()
        manager.ensure_dir()
        assert profile_dir.exists()


class TestGetInfo:
    def test_nonexistent_profile(self, manager):
        info = manager.get_info()
        assert not info.exists
        assert info.size_mb == 0.0
        assert info.sessions == []

    def test_populated_profile(self, populated_profile):
        info = populated_profile.get_info()
        assert info.exists
        assert info.size_mb > 0
        assert len(info.sessions) == 3
        domains = {s.domain for s in info.sessions}
        assert "github.com" in domains
        assert "google.com" in domains
        assert "example.com" in domains

    def test_cookie_counts(self, populated_profile):
        info = populated_profile.get_info()
        github = next(s for s in info.sessions if s.domain == "github.com")
        assert github.cookie_count == 2
        google = next(s for s in info.sessions if s.domain == "google.com")
        assert google.cookie_count == 2


class TestClearDomain:
    def test_clears_specific_domain(self, populated_profile):
        result = populated_profile.clear_domain("github.com")
        assert result is True

        info = populated_profile.get_info()
        domains = {s.domain for s in info.sessions}
        assert "github.com" not in domains
        assert "google.com" in domains

    def test_returns_false_for_nonexistent(self, populated_profile):
        result = populated_profile.clear_domain("nonexistent.com")
        assert result is False

    def test_no_profile_returns_false(self, manager):
        result = manager.clear_domain("github.com")
        assert result is False


class TestExportState:
    def test_exports_cookies(self, populated_profile, tmp_path):
        dest = tmp_path / "state.json"
        result = populated_profile.export_state(dest)
        assert result == dest
        assert dest.exists()

        state = json.loads(dest.read_text())
        assert "cookies" in state
        assert len(state["cookies"]) == 5

    def test_empty_profile(self, manager, tmp_path):
        manager.ensure_dir()
        dest = tmp_path / "state.json"
        manager.export_state(dest)

        state = json.loads(dest.read_text())
        assert state["cookies"] == []


class TestBackup:
    def test_backup_creates_copy(self, populated_profile, tmp_path):
        dest = tmp_path / "backups"
        result = populated_profile.backup(dest)
        assert result is not None
        assert result.exists()
        assert (result / "Default" / "Cookies").exists()

    def test_no_profile_returns_none(self, manager, tmp_path):
        result = manager.backup(tmp_path / "backups")
        assert result is None


class TestReset:
    def test_deletes_profile(self, populated_profile):
        assert populated_profile.profile_dir.exists()
        result = populated_profile.reset()
        assert result is True
        assert not populated_profile.profile_dir.exists()

    def test_no_profile_returns_false(self, manager):
        result = manager.reset()
        assert result is False


class TestBrowserLayerEnum:
    def test_layer_values(self):
        assert BrowserLayer.FETCH == "fetch"
        assert BrowserLayer.MANAGED == "managed"
        assert BrowserLayer.RELAY == "relay"
