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
    assert cfg.items_per_eval == 1
    assert cfg.model == "sonnet"
    assert cfg.effort == "high"
    assert cfg.timeout_s == 1200
    assert cfg.enabled is True
    assert cfg.max_retries == 3
    assert cfg.recursive is False
    # The gate defaults to ENFORCE through the config LAYER, not only through the
    # dataclass — the path an install takes whose inbox_monitor.yaml predates the
    # key. Deliberately changed from shadow once measured: 0 of 42 evaluations
    # under the **Source:** contract would have been re-queued, and parking now
    # alerts the owner, so a false flag is visible rather than a silent stall.
    assert cfg.url_coverage_mode == "enforce"


def test_invalid_url_coverage_mode_coerces_to_shadow_loudly(caplog):
    """An unrecognised value already degraded to shadow, but SILENTLY.

    The monitor reads this as `!= "enforce"`, so a typo would leave the gate
    observing forever while the operator believed it was live — and they only
    touch this lever once they have decided to act on the shadow measurement.
    The MCP settings validator rejects a bad value on its own path; this covers
    a hand-edited YAML or a local overlay, which that validator never sees.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        cfg = load_inbox_config_from_string("""
inbox_monitor:
  watch_path: "/tmp/test"
  url_coverage_mode: "enfoce"
""")
    assert cfg.url_coverage_mode == "shadow"
    assert any("url_coverage_mode" in r.getMessage() for r in caplog.records), (
        "an unrecognised mode must SAY it is falling back, not degrade in silence"
    )


def test_a_valid_url_coverage_mode_survives():
    cfg = load_inbox_config_from_string("""
inbox_monitor:
  watch_path: "/tmp/test"
  url_coverage_mode: "enforce"
""")
    assert cfg.url_coverage_mode == "enforce"


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


def test_defaults_have_one_source_of_truth():
    """#1953: every parse default must equal the dataclass default, so the two
    can never drift apart again (timeout_s once read 3600/600/900 in three
    places). A minimal config parses to exactly the dataclass defaults."""
    import dataclasses

    from genesis.inbox.types import InboxConfig

    cfg = load_inbox_config_from_string('inbox_monitor:\n  watch_path: "/tmp/x"\n')
    for f in dataclasses.fields(InboxConfig):
        if f.name == "watch_path":
            continue
        assert getattr(cfg, f.name) == f.default, f.name


def test_coverage_gate_defaults_to_enforce():
    cfg = load_inbox_config_from_string('inbox_monitor:\n  watch_path: "/tmp/x"\n')
    assert cfg.url_coverage_mode == "enforce"


def test_shadow_remains_selectable():
    cfg = load_inbox_config_from_string(
        'inbox_monitor:\n  watch_path: "/tmp/x"\n  url_coverage_mode: shadow\n'
    )
    assert cfg.url_coverage_mode == "shadow"


@pytest.mark.parametrize("key", ["items_per_eval", "max_retries", "timeout_s",
                                 "check_interval_seconds"])
@pytest.mark.parametrize("bad", [0, -3])
def test_non_positive_int_falls_back_to_default(key, bad, caplog):
    """A hand-edited 0 or negative used to be coerced with a bare int() and
    accepted: items_per_eval=0 would divide a drop into nothing."""
    import dataclasses

    from genesis.inbox.types import InboxConfig

    default = {f.name: f.default for f in dataclasses.fields(InboxConfig)}[key]
    cfg = load_inbox_config_from_string(
        f'inbox_monitor:\n  watch_path: "/tmp/x"\n  {key}: {bad}\n'
    )
    assert getattr(cfg, key) == default
    assert key in caplog.text


@pytest.mark.parametrize("key", ["items_per_eval", "max_retries"])
@pytest.mark.parametrize("bad", ["true", "2.7", ".inf", "-.inf", ".nan"])
def test_non_integer_int_falls_back_to_default(key, bad, caplog):
    """#2447 round 1 (Devin, CodeRabbit): int() turned YAML `true` into 1 and
    truncated 2.7 to 2 without a word, and `.inf` raised OverflowError, which
    crashed config loading instead of degrading to the default."""
    import dataclasses

    from genesis.inbox.types import InboxConfig

    default = {f.name: f.default for f in dataclasses.fields(InboxConfig)}[key]
    cfg = load_inbox_config_from_string(
        f'inbox_monitor:\n  watch_path: "/tmp/x"\n  {key}: {bad}\n'
    )
    assert getattr(cfg, key) == default
    assert key in caplog.text


def test_integral_float_is_accepted_as_an_int():
    """Negative control: 4.0 is an integer written as a float, not an error."""
    cfg = load_inbox_config_from_string(
        'inbox_monitor:\n  watch_path: "/tmp/x"\n  max_retries: 4.0\n'
    )
    assert cfg.max_retries == 4 and isinstance(cfg.max_retries, int)
