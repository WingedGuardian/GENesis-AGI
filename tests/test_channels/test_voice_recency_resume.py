"""Tests for the voice cross-session recency-resume config lever + validator.

Mirrors the ``voice_act`` lever test style: pin the reader at a tmp yaml and
clear the env kill switch, then assert the fail-safe behavior of every key.
"""

from __future__ import annotations

import pytest

from genesis.channels.voice import voice_recency_resume_config as cfg


@pytest.fixture
def pin(tmp_path, monkeypatch):
    """Point the config reader at a tmp yaml and clear the env kill switch."""
    p = tmp_path / "voice_recency_resume.yaml"
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)

    def _write(text: str) -> None:
        p.write_text(text)

    return _write


def test_defaults_when_no_file(pin):
    r = cfg.resolved()
    assert r["mode"] == "off"
    assert r["scope"] == "global"
    assert r["max_turns"] == 6
    assert r["max_chars"] == 800
    assert r["max_age_hours"] is None


def test_mode_live(pin):
    pin("mode: live\n")
    assert cfg.effective_mode() == "live"


def test_mode_off_yaml_boolean(pin):
    # YAML 1.1 parses unquoted `off` as boolean False — must coerce to "off".
    pin("mode: off\n")
    assert cfg.effective_mode() == "off"


def test_env_kill_forces_off(pin, monkeypatch):
    pin("mode: live\n")
    monkeypatch.setenv(cfg._ENV_KILL_SWITCH, "1")
    assert cfg.effective_mode() == "off"


def test_enabled_false_forces_off(pin):
    pin("enabled: false\nmode: live\n")
    assert cfg.effective_mode() == "off"


def test_enabled_string_false_is_rejected(pin):
    # "false" is a truthy non-bool → fail-closed to off (never inject on bad cfg).
    pin('enabled: "false"\nmode: live\n')
    assert cfg.effective_mode() == "off"


def test_invalid_mode_off(pin):
    pin("mode: bogus\n")
    assert cfg.effective_mode() == "off"


def test_scope_valid_and_invalid(pin):
    pin("mode: live\nscope: per_device\n")
    assert cfg.resolved()["scope"] == "per_device"
    pin("mode: live\nscope: sideways\n")
    assert cfg.resolved()["scope"] == "global"


def test_max_turns_and_chars_fail_safe(pin):
    pin("mode: live\nmax_turns: 3\nmax_chars: 200\n")
    r = cfg.resolved()
    assert r["max_turns"] == 3
    assert r["max_chars"] == 200
    pin("mode: live\nmax_turns: 0\nmax_chars: -5\n")
    r = cfg.resolved()
    assert r["max_turns"] == 6  # non-positive → default
    assert r["max_chars"] == 800
    pin("mode: live\nmax_turns: true\n")
    assert cfg.resolved()["max_turns"] == 6  # bool rejected


def test_max_age_hours(pin):
    pin("mode: live\nmax_age_hours: 12\n")
    assert cfg.resolved()["max_age_hours"] == 12.0
    pin("mode: live\nmax_age_hours: null\n")
    assert cfg.resolved()["max_age_hours"] is None
    pin("mode: live\nmax_age_hours: -3\n")
    assert cfg.resolved()["max_age_hours"] is None  # non-positive → no limit


# --- settings validator ---


def test_validator_accepts_valid():
    from genesis.mcp.health.settings import _validate_voice_recency_resume as v

    assert (
        v(
            {
                "mode": "live",
                "scope": "per_device",
                "max_turns": 4,
                "max_chars": 500,
                "max_age_hours": 6,
            }
        )
        == []
    )
    assert v({"max_age_hours": None}) == []
    assert v({"enabled": True}) == []


def test_validator_rejects_bad():
    from genesis.mcp.health.settings import _validate_voice_recency_resume as v

    assert v({"bogus": 1})  # unknown key
    assert v({"enabled": "false"})  # non-bool
    assert v({"mode": "sideways"})
    assert v({"scope": "elsewhere"})
    assert v({"max_turns": 0})
    assert v({"max_turns": True})  # bool rejected as int
    assert v({"max_chars": -1})
    assert v({"max_age_hours": -2})
    assert v({"max_age_hours": True})
