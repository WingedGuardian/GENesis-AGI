"""Tests for mail config loading."""

import tempfile
from pathlib import Path

from genesis.mail.config import load_mail_config


def test_load_full_config():
    yaml_content = """\
mail_monitor:
  enabled: true
  cron_expression: "0 6 * * 1"
  batch_size: 5
  model: "opus"
  effort: "high"
  timeout_s: 900
  max_retries: 5
  imap_timeout_s: 60
  max_emails_per_run: 100
  timezone: "UTC"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_mail_config(Path(f.name))

    assert cfg.enabled is True
    assert cfg.cron_expression == "0 6 * * 1"
    assert cfg.batch_size == 5
    assert cfg.model == "opus"
    assert cfg.timezone == "UTC"


def test_load_defaults_on_empty():
    yaml_content = "mail_monitor: {}\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_mail_config(Path(f.name))

    assert cfg.enabled is True
    assert cfg.cron_expression == "0 5 * * 0"
    assert cfg.batch_size == 10


def test_load_missing_section_uses_root():
    yaml_content = "enabled: false\nbatch_size: 3\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_mail_config(Path(f.name))

    assert cfg.enabled is False
    assert cfg.batch_size == 3
