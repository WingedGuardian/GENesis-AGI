"""Tests for bridge config loading."""

import pytest

from genesis.channels.bridge import _load_bridge_config


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    """Create a temp secrets.env and point the loader at it."""
    path = tmp_path / "secrets.env"
    monkeypatch.setattr("genesis.channels.bridge.secrets_path", lambda: path)
    return path


def test_load_bridge_config_parses_values(secrets_file):
    secrets_file.write_text(
        'TELEGRAM_BOT_TOKEN=abc123\n'
        'TELEGRAM_ALLOWED_USERS=111,222\n'
        'WHISPER_MODEL=whisper-tiny\n'
        'DAY_BOUNDARY_HOUR=5\n'
        'API_KEY_GROQ=some-key\n'
    )
    config = _load_bridge_config()
    assert config["token"] == "abc123"
    assert config["allowed_users"] == {111, 222}
    assert config["whisper_model"] == "whisper-tiny"
    assert config["day_boundary_hour"] == 5


def test_load_bridge_config_defaults(secrets_file):
    secrets_file.write_text('TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_ALLOWED_USERS=123\n')
    config = _load_bridge_config()
    assert config["token"] == "tok"
    assert config["allowed_users"] == {123}
    assert config["whisper_model"] == "whisper-large-v3"
    assert config["day_boundary_hour"] == 0


def test_load_bridge_config_empty_users_returns_none(secrets_file):
    """Token set but no allowed users → None (refuses to start)."""
    secrets_file.write_text('TELEGRAM_BOT_TOKEN=tok\n')
    assert _load_bridge_config() is None


def test_load_bridge_config_invalid_uids_returns_none(secrets_file):
    """Token set but allowed_users contains non-numeric values → None."""
    secrets_file.write_text(
        'TELEGRAM_BOT_TOKEN=tok\n'
        'TELEGRAM_ALLOWED_USERS=123456:ABC-token\n'
    )
    assert _load_bridge_config() is None


def test_load_bridge_config_missing_token_returns_none(secrets_file):
    """Missing token → None (headless mode), not SystemExit."""
    secrets_file.write_text('SOME_KEY=value\n')
    assert _load_bridge_config() is None


def test_load_bridge_config_placeholder_token_returns_none(secrets_file):
    """Placeholder token → None (headless mode), not SystemExit."""
    secrets_file.write_text('TELEGRAM_BOT_TOKEN=PLACEHOLDER\n')
    assert _load_bridge_config() is None


def test_load_bridge_config_missing_file(monkeypatch):
    monkeypatch.setattr(
        "genesis.channels.bridge.secrets_path",
        lambda: "/nonexistent/secrets.env",
    )
    with pytest.raises(SystemExit):
        _load_bridge_config()
