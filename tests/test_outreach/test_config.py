"""Tests for outreach configuration loader."""

import tempfile
from pathlib import Path

from genesis.outreach.config import OutreachConfig, QuietHours, load_outreach_config


def test_default_config():
    config = load_outreach_config(Path("/nonexistent"))
    assert isinstance(config, OutreachConfig)
    assert config.quiet_hours.start == "22:00"
    assert config.quiet_hours.end == "07:00"
    assert config.max_daily == 5
    assert config.surplus_daily == 1


def test_load_from_yaml():
    yaml_content = """
quiet_hours:
  start: "23:00"
  end: "08:00"
  timezone: "US/Eastern"
channel_preferences:
  default: telegram
thresholds:
  blocker: 0.0
  alert: 0.2
  surplus: 0.8
  digest: 0.0
rate_limits:
  max_daily: 3
  surplus_daily: 1
morning_report:
  trigger_time: "06:30"
  timezone: "US/Eastern"
engagement:
  timeout_hours: 48
  poll_interval_minutes: 30
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        config = load_outreach_config(Path(f.name))

    assert config.quiet_hours.start == "23:00"
    assert config.max_daily == 3
    assert config.morning_report_time == "06:30"
    assert config.engagement_timeout_hours == 48


def test_quiet_hours_dataclass():
    qh = QuietHours(start="22:00", end="07:00")
    assert qh.start == "22:00"
    assert qh.end == "07:00"
