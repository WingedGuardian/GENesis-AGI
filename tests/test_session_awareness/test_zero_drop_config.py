"""The zero-drop lever must fail toward LESS egress, never toward silence.

A detector that degrades to ``off`` on a config typo keeps answering "what fell
through the cracks?" with a stale, confident zero — and nothing in the system
would say otherwise, because an empty board and a dead detector look identical
from the outside. So an invalid mode lands on ``observe``: the board still
fills, only the alert stops.
"""

import pytest

from genesis.session_awareness import zero_drop_config as cfg_mod


@pytest.fixture
def cfg_root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(cfg_mod, "repo_root", lambda: tmp_path)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))
    return tmp_path / "config" / "zero_drop.yaml"


def test_defaults_when_no_config_file_exists(cfg_root):
    assert cfg_mod.effective_mode() == "observe"
    assert cfg_mod.load_config()["min_interval_minutes"] == 60


@pytest.mark.parametrize("value", ["banana", "", 3, None])
def test_invalid_mode_degrades_to_observe_not_off(cfg_root, value):
    """Also pins that the degrade target is not `off`: a silently-off detector
    keeps answering with a stale, confident zero."""
    cfg_root.write_text(f"mode: {value!r}\n")
    assert cfg_mod.effective_mode() == "observe"


def test_yaml_boolean_off_is_honored_as_off(cfg_root):
    """An unquoted `mode: off` parses as YAML-1.1 False. That intent is
    unambiguous — honor it rather than degrading it to observe."""
    cfg_root.write_text("mode: off\n")
    assert cfg_mod.effective_mode() == "off"


def test_master_switch_beats_the_mode(cfg_root):
    cfg_root.write_text("enabled: false\nmode: alert\n")
    assert cfg_mod.effective_mode() == "off"


def test_corrupt_config_degrades_to_defaults(cfg_root):
    cfg_root.write_text("mode: [unclosed\n")
    assert cfg_mod.effective_mode() == "observe"
    assert cfg_mod.load_config()["escalation_k"] == 3


@pytest.mark.parametrize("bad", [0, -1, True, "3", 2.5, None])
def test_damaged_int_knob_falls_back_rather_than_zeroing_a_limit(bad):
    """A zeroed max_prs would return an EMPTY PR history, which classifies
    every merged branch as stranded — the config-damage path must not be able
    to manufacture findings."""
    assert cfg_mod.knob_int({"max_prs": bad}, "max_prs") == cfg_mod.DEFAULTS["max_prs"]


def test_int_knob_accepts_a_real_override():
    assert cfg_mod.knob_int({"escalation_k": 5}, "escalation_k") == 5


@pytest.mark.parametrize("bad", ["urgent", 1, None, ""])
def test_alert_priority_is_a_closed_set(bad):
    assert cfg_mod.alert_priority({"alert_priority": bad}) == "medium"


def test_alert_priority_accepts_a_valid_value():
    assert cfg_mod.alert_priority({"alert_priority": "high"}) == "high"


def test_shipped_config_file_parses_and_matches_the_defaults():
    """The shipped yaml is what every fresh clone gets. If it drifts from
    DEFAULTS, the documented behaviour and the actual behaviour disagree."""
    import yaml

    from genesis.env import repo_root

    shipped = yaml.safe_load((repo_root() / "config" / "zero_drop.yaml").read_text())
    for key, value in shipped.items():
        assert key in cfg_mod.DEFAULTS, f"shipped config has an unknown key {key!r}"
        assert value == cfg_mod.DEFAULTS[key], (
            f"{key}: shipped={value!r} default={cfg_mod.DEFAULTS[key]!r}"
        )


def test_settings_validator_rejects_what_the_loader_would_reject():
    """The settings domain and the loader must agree — a value the validator
    accepts but the loader silently replaces is a lever that lies."""
    from genesis.mcp.health.settings import _validate_zero_drop

    assert _validate_zero_drop({"mode": "alert", "escalation_k": 5}) == []
    assert _validate_zero_drop({"mode": "banana"})
    assert _validate_zero_drop({"escalation_k": 0})
    assert _validate_zero_drop({"enabled": "yes"})
    assert _validate_zero_drop({"alert_priority": "urgent"})
    assert _validate_zero_drop({"not_a_knob": 1})
