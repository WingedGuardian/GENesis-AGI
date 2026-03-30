"""Tests for inbox config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.inbox.config import load_inbox_config_from_string


def test_full_config():
    cfg = load_inbox_config_from_string("""
inbox_monitor:
  enabled: true
  watch_path: "/tmp/inbox"
  response_dir: "_genesis"
  check_interval_seconds: 900
  batch_size: 10
  model: "opus"
  effort: "high"
  timeout_s: 300
""")
    assert cfg.watch_path == Path("/tmp/inbox")
    assert cfg.check_interval_seconds == 900
    assert cfg.batch_size == 10
    assert cfg.model == "opus"
    assert cfg.effort == "high"
    assert cfg.enabled is True


def test_defaults():
    cfg = load_inbox_config_from_string("""
inbox_monitor:
  watch_path: "/tmp/test"
""")
    assert cfg.response_dir == "_genesis"
    assert cfg.check_interval_seconds == 1800
    assert cfg.batch_size == 5
    assert cfg.model == "sonnet"
    assert cfg.effort == "high"
    assert cfg.timeout_s == 600
    assert cfg.enabled is True
    assert cfg.max_retries == 3
    assert cfg.recursive is False


def test_new_config_fields():
    cfg = load_inbox_config_from_string("""
inbox_monitor:
  watch_path: "/tmp/test"
  max_retries: 5
  recursive: true
""")
    assert cfg.max_retries == 5
    assert cfg.recursive is True


def test_missing_watch_path_raises():
    with pytest.raises(KeyError, match="watch_path"):
        load_inbox_config_from_string("""
inbox_monitor:
  enabled: true
""")


def test_missing_section_raises():
    with pytest.raises(ValueError, match="inbox_monitor"):
        load_inbox_config_from_string("""
something_else:
  key: value
""")


def test_invalid_yaml_type_raises():
    with pytest.raises(ValueError, match="YAML mapping"):
        load_inbox_config_from_string("just a string")
