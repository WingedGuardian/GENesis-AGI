"""Tests for BrowserProfileManager."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from genesis.browser.profile import BrowserProfileManager, ProfileInUse
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
        assert result == 2

        info = populated_profile.get_info()
        domains = {s.domain for s in info.sessions}
        assert "github.com" not in domains
        assert "google.com" in domains

    def test_returns_zero_for_nonexistent(self, populated_profile):
        assert populated_profile.clear_domain("nonexistent.com") == 0

    def test_no_profile_returns_zero(self, manager):
        assert manager.clear_domain("github.com") == 0

    def test_matches_whole_labels_never_substrings(self, profile_dir):
        """`x.com` must not clear netflix.com (the LIKE %x.com% bug)."""
        mgr = _chromium_profile(profile_dir, [
            ".x.com", "x.com", "api.x.com", ".netflix.com", "box.com", "x.com.evil.example",
        ])
        assert mgr.clear_domain("x.com") == 3
        left = {s.domain for s in mgr.get_info().sessions}
        assert left == {"netflix.com", "box.com", "x.com.evil.example"}

    def test_rejects_a_non_domain(self, populated_profile):
        with pytest.raises(ValueError):
            populated_profile.clear_domain("%")
        with pytest.raises(ValueError):
            populated_profile.clear_domain("  ")

    def test_refuses_while_a_browser_has_the_profile_open(self, populated_profile):
        lock = populated_profile.profile_dir / "SingletonLock"
        os.symlink(f"somehost-{os.getpid()}", lock)
        with pytest.raises(ProfileInUse):
            populated_profile.clear_domain("github.com")
        assert {s.domain for s in populated_profile.get_info().sessions} >= {"github.com"}

    def test_a_stale_lock_from_a_crash_does_not_block(self, populated_profile):
        os.symlink("somehost-999999999", populated_profile.profile_dir / "SingletonLock")
        assert populated_profile.running_pid() is None
        assert populated_profile.clear_domain("github.com") == 2


def _chromium_profile(profile_dir, hosts):
    (profile_dir / "Default").mkdir(parents=True)
    conn = sqlite3.connect(str(profile_dir / "Default" / "Cookies"))
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, path TEXT, "
                 "is_secure INTEGER, is_httponly INTEGER)")
    conn.executemany("INSERT INTO cookies VALUES (?, 'n', 'v', '/', 0, 0)", [(h,) for h in hosts])
    conn.commit()
    conn.close()
    return BrowserProfileManager(profile_dir)


def _camoufox_profile(profile_dir, hosts):
    profile_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(profile_dir / "cookies.sqlite"))
    conn.execute("PRAGMA journal_mode=wal")
    conn.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT, value TEXT, host TEXT, path TEXT)"
    )
    conn.executemany(
        "INSERT INTO moz_cookies (name, value, host, path) VALUES ('n', 'v', ?, '/')",
        [(h,) for h in hosts],
    )
    conn.commit()
    conn.close()
    return BrowserProfileManager(profile_dir, browser="camoufox")


class TestCamoufoxProfile:
    def test_lists_firefox_cookies(self, tmp_path):
        mgr = _camoufox_profile(
            tmp_path / "camoufox-profile", [".github.com", "github.com", ".medium.com"]
        )
        info = mgr.get_info()
        assert info.browser == "camoufox"
        assert {(s.domain, s.cookie_count) for s in info.sessions} == {
            ("github.com", 2), ("medium.com", 1),
        }

    def test_clears_firefox_cookies_with_a_count(self, tmp_path):
        mgr = _camoufox_profile(tmp_path / "camoufox-profile", [".x.com", "api.x.com", ".netflix.com"])
        assert mgr.clear_domain("x.com") == 2
        assert {s.domain for s in mgr.get_info().sessions} == {"netflix.com"}

    def test_firefox_lock_link_marks_the_profile_in_use(self, tmp_path):
        mgr = _camoufox_profile(tmp_path / "camoufox-profile", [".x.com"])
        os.symlink(f"127.0.1.1:+{os.getpid()}", mgr.profile_dir / "lock")
        assert mgr.running_pid() == os.getpid()
        with pytest.raises(ProfileInUse):
            mgr.clear_domain("x.com")

    def test_an_unreadable_store_is_reported_not_empty(self, tmp_path):
        d = tmp_path / "camoufox-profile"
        d.mkdir()
        (d / "cookies.sqlite").write_bytes(b"not a database" * 100)
        info = BrowserProfileManager(d, browser="camoufox").get_info()
        assert info.sessions == []
        assert "unreadable" in info.error


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
    def test_layer_values_are_the_navigate_layer_field(self):
        """One numbering everywhere: the enum, browser.py's "Layer N" comments
        and the ``layer`` field browser_navigate returns."""
        assert [m.value for m in BrowserLayer] == [
            "camoufox", "chromium", "remote_cdp", "tinyfish_cdp",
        ]

    def test_browser_py_layer_comments_match_the_enum_order(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[2] / "src/genesis/mcp/health/browser.py"
        ).read_text()
        for n, name in enumerate(("Camoufox", "Chromium", "CDP remote", "TinyFish"), 1):
            assert f"# Layer {n}: {name}" in src, (n, name)
