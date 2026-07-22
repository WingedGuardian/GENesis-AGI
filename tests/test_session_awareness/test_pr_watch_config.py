"""Tests for the PR-watch config lever."""

from __future__ import annotations

import genesis.session_awareness.pr_watch_config as cfgmod


def test_defaults_when_no_files(monkeypatch, tmp_path):
    # repo_root has no config/pr_watch.yaml -> pure DEFAULTS.
    monkeypatch.setattr(cfgmod, "repo_root", lambda: tmp_path)
    cfg = cfgmod.load_config()
    assert cfg["enabled"] is True
    assert cfg["lookback_days"] == 30
    assert cfg["resurface_days"] == 10
    assert cfg["max_surface"] == 5


def test_base_yaml_overrides_defaults(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pr_watch.yaml").write_text("enabled: false\nresurface_days: 3\n")
    monkeypatch.setattr(cfgmod, "repo_root", lambda: tmp_path)
    cfg = cfgmod.load_config()
    assert cfg["enabled"] is False
    assert cfg["resurface_days"] == 3
    assert cfg["lookback_days"] == 30  # untouched default


def test_is_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(cfgmod, "repo_root", lambda: tmp_path)
    assert cfgmod.is_enabled({"enabled": True}) is True
    assert cfgmod.is_enabled({"enabled": False}) is False
    assert cfgmod.is_enabled({}) is True  # default-on


def test_knob_int_rejects_bad_values():
    assert cfgmod.knob_int({"lookback_days": 0}, "lookback_days") == 30
    assert cfgmod.knob_int({"lookback_days": -5}, "lookback_days") == 30
    assert cfgmod.knob_int({"lookback_days": True}, "lookback_days") == 30
    assert cfgmod.knob_int({"lookback_days": "x"}, "lookback_days") == 30
    assert cfgmod.knob_int({"max_surface": 7}, "max_surface") == 7
