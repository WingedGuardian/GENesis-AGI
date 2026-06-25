"""Tests for the shared recon watchlist store (base + overlay + tombstones)."""

import pytest
import yaml

from genesis.recon import watchlist


@pytest.fixture
def wl(tmp_path, monkeypatch):
    """Point the store at temp base/overlay files with a 2-entry base list."""
    base = tmp_path / "recon_watchlist.yaml"
    base.write_text(yaml.safe_dump({"projects": [
        {"name": "Claude Code", "repo": "anthropics/claude-code",
         "track": ["releases"], "priority": "high"},
        {"name": "OpenClaw", "repo": "openclaw/openclaw",
         "track": ["commits"], "priority": "medium"},
    ]}))
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", base)
    monkeypatch.setattr(watchlist, "LOCAL_PATH",
                        tmp_path / "recon_watchlist.local.yaml")
    return watchlist


def _repos(entries):
    return [e["repo"] for e in entries]


def test_active_entries_base_only(wl):
    assert _repos(wl.active_entries()) == [
        "anthropics/claude-code", "openclaw/openclaw"]


def test_add_repo_appends_and_persists(wl):
    r = wl.add_repo({"name": "Foo", "repo": "acme/foo",
                     "track": ["releases", "stars"], "priority": "low"})
    assert r == {"ok": True, "repo": "acme/foo"}
    assert "acme/foo" in _repos(wl.active_entries())
    assert wl.LOCAL_PATH.exists()           # overlay written
    # Survives a re-read (atomic write produced valid YAML).
    assert "acme/foo" in _repos(wl.active_entries())


def test_add_repo_dedup_against_base_and_overlay(wl):
    assert "error" in wl.add_repo({"name": "x", "repo": "openclaw/openclaw",
                                   "track": ["commits"], "priority": "low"})
    wl.add_repo({"name": "Foo", "repo": "acme/foo",
                 "track": ["releases"], "priority": "low"})
    assert "error" in wl.add_repo({"name": "y", "repo": "acme/foo",
                                   "track": ["releases"], "priority": "low"})


@pytest.mark.parametrize("entry", [
    {"name": "", "repo": "a/b", "track": ["commits"], "priority": "low"},
    {"name": "x", "repo": "not-a-repo", "track": ["commits"], "priority": "low"},
    {"name": "x", "repo": "https://github.com/a/b", "track": ["commits"], "priority": "low"},
    {"name": "x", "repo": "a/b", "track": [], "priority": "low"},
    {"name": "x", "repo": "a/b", "track": ["bogus"], "priority": "low"},
    {"name": "x", "repo": "a/b", "track": ["commits"], "priority": "urgent"},
    {"name": "x", "repo": "a/b", "track": ["commits"], "priority": "low",
     "urls": ["http://insecure"]},
])
def test_add_repo_validation_rejects(wl, entry):
    assert "error" in wl.add_repo(entry)


def test_disable_base_tombstones_and_reenable(wl):
    r = wl.set_base_disabled("openclaw/openclaw", True)
    assert r["disabled"] is True
    assert "openclaw/openclaw" not in _repos(wl.active_entries())
    # Still visible in the editor view, flagged disabled.
    le = {e["repo"]: e for e in wl.list_entries()}
    assert le["openclaw/openclaw"]["disabled"] is True
    assert le["openclaw/openclaw"]["source"] == "base"
    # Re-enable restores it.
    wl.set_base_disabled("openclaw/openclaw", False)
    assert "openclaw/openclaw" in _repos(wl.active_entries())


def test_disable_rejects_non_base(wl):
    wl.add_repo({"name": "Foo", "repo": "acme/foo",
                 "track": ["releases"], "priority": "low"})
    assert "error" in wl.set_base_disabled("acme/foo", True)


def test_remove_overlay_repo(wl):
    wl.add_repo({"name": "Foo", "repo": "acme/foo",
                 "track": ["releases"], "priority": "low"})
    assert wl.remove_overlay_repo("acme/foo")["ok"] is True
    assert "acme/foo" not in _repos(wl.active_entries())


def test_remove_rejects_base_entry(wl):
    # Base entries can only be disabled, never deleted via the overlay.
    assert "error" in wl.remove_overlay_repo("openclaw/openclaw")


def test_list_entries_marks_source(wl):
    wl.add_repo({"name": "Foo", "repo": "acme/foo",
                 "track": ["releases"], "priority": "low"})
    src = {e["repo"]: e["source"] for e in wl.list_entries()}
    assert src["anthropics/claude-code"] == "base"
    assert src["acme/foo"] == "overlay"


# ── loader unification (the bug this PR closes) ───────────────────────


def test_gatherer_loader_sees_overlay(wl):
    from genesis.recon.gatherer import ReconGatherer
    wl.add_repo({"name": "Foo", "repo": "acme/foo",
                 "track": ["releases"], "priority": "low"})
    assert "acme/foo" in _repos(ReconGatherer._load_watchlist())


def test_mcp_loader_respects_tombstone(wl):
    from genesis.mcp.recon_mcp import _load_watchlist
    wl.set_base_disabled("openclaw/openclaw", True)
    # Previously the MCP loader read base-only and would still show this.
    assert "openclaw/openclaw" not in _repos(_load_watchlist())
