"""Tests for resilience config loader."""

from textwrap import dedent

from genesis.resilience.config import (
    ResilienceConfig,
    load_config,
)


def test_load_config_missing_file(tmp_path):
    """Missing file returns defaults."""
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert cfg == ResilienceConfig()


def test_load_config_valid_yaml(tmp_path):
    """Valid YAML is parsed correctly."""
    p = tmp_path / "resilience.yaml"
    p.write_text(dedent("""\
        flapping:
          transition_count: 5
          window_seconds: 1200
          stabilization_seconds: 300
        recovery:
          confirmation_probes: 5
        cc:
          max_sessions_per_hour: 30
          throttle_threshold_pct: 0.90
        status:
          path: "/tmp/status.json"
          write_every_tick: false
        notifications:
          webhook_url: "https://example.com/hook"
          webhook_events:
            - state_changed
    """))
    cfg = load_config(p)
    assert cfg.flapping.transition_count == 5
    assert cfg.flapping.window_seconds == 1200
    assert cfg.recovery.confirmation_probes == 5
    assert cfg.recovery.drain_pace_s == 30  # default preserved
    assert cfg.cc.max_sessions_per_hour == 30
    assert cfg.cc.throttle_threshold_pct == 0.90
    assert cfg.status.path == "/tmp/status.json"
    assert cfg.status.write_every_tick is False
    assert cfg.notifications.webhook_url == "https://example.com/hook"
    assert cfg.notifications.webhook_events == ["state_changed"]


def test_default_values():
    """Default config values are correct."""
    cfg = ResilienceConfig()
    assert cfg.flapping.transition_count == 3
    assert cfg.flapping.window_seconds == 900
    assert cfg.flapping.stabilization_seconds == 600
    assert cfg.recovery.confirmation_probes == 3
    assert cfg.recovery.confirmation_interval_s == 30
    assert cfg.recovery.drain_pace_s == 30
    assert cfg.recovery.embedding_pace_per_min == 10
    assert cfg.recovery.queue_overflow_threshold == 1000
    assert cfg.cc.max_sessions_per_hour == 20
    assert cfg.cc.throttle_threshold_pct == 0.80
    assert cfg.status.path == "~/.genesis/status.json"
    assert cfg.status.write_every_tick is True
    assert cfg.notifications.webhook_url is None
    assert cfg.notifications.webhook_events == ["state_changed", "recovery_complete", "queue_overflow"]


def test_load_config_empty_yaml(tmp_path):
    """Empty YAML returns defaults."""
    p = tmp_path / "empty.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert cfg == ResilienceConfig()
