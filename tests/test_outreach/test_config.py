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


def test_default_voice_alert_ids_task_signals():
    """The voice allowlist speaks attention-worthy task notifications
    (task_complete / task_alert) but not routine progress; the old catch-all
    task_notification signal is gone."""
    config = load_outreach_config(Path("/nonexistent"))
    assert "task_complete" in config.voice_alert_ids
    assert "task_alert" in config.voice_alert_ids
    assert "task_progress" not in config.voice_alert_ids
    assert "task_notification" not in config.voice_alert_ids


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


def test_marketing_defaults_survive_a_pre_marketing_saved_config():
    """A config saved BEFORE the marketing category existed carries thresholds/
    channel_preferences/delivery_routing maps that lack a 'marketing' key. The
    loader must merge the shipped defaults UNDER those maps so marketing still
    resolves to its shipped values (always-deliver threshold, telegram, the
    dedicated supergroup topic) instead of falling back to generic defaults."""
    yaml_content = """
channel_preferences:
  default: discord
  blocker: telegram
thresholds:
  blocker: 0.0
  digest: 0.0
delivery_routing:
  default: dm
  approval: supergroup
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        path = Path(f.name)
    try:
        config = load_outreach_config(path)
        # marketing threshold present despite the saved thresholds block omitting it
        assert config.thresholds["marketing"] == 0.0
        # user overrides still win (they set blocker/digest, default channel discord)
        assert config.channel_preferences["default"] == "discord"
        # marketing routes to telegram + the supergroup topic even though the saved
        # config's default channel is discord and default routing is dm
        assert config.channel_preferences["marketing"] == "telegram"
        assert config.delivery_routing["marketing"] == "supergroup"
        assert config.delivery_routing["default"] == "dm"  # user override preserved
    finally:
        path.unlink()
