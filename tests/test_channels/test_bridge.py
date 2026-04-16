"""Tests for bridge config loading."""

import os
import tempfile

from genesis.channels.bridge import _load_bridge_config


def _write_secrets(content: str) -> str:
    """Write a temporary secrets file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".env")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


class TestLoadBridgeConfig:
    def test_forum_chat_id_parsed(self, monkeypatch):
        path = _write_secrets(
            'TELEGRAM_BOT_TOKEN=testtoken\n'
            'TELEGRAM_ALLOWED_USERS=12345\n'
            'TELEGRAM_FORUM_CHAT_ID=-100123456\n'
        )
        monkeypatch.setattr(
            "genesis.channels.bridge.secrets_path", lambda: path,
        )
        config = _load_bridge_config()
        assert config["forum_chat_id"] == -100123456
        os.unlink(path)

    def test_forum_chat_id_absent(self, monkeypatch):
        path = _write_secrets(
            'TELEGRAM_BOT_TOKEN=testtoken\n'
            'TELEGRAM_ALLOWED_USERS=12345\n'
        )
        monkeypatch.setattr(
            "genesis.channels.bridge.secrets_path", lambda: path,
        )
        config = _load_bridge_config()
        assert config["forum_chat_id"] is None
        os.unlink(path)

    def test_forum_chat_id_empty_string(self, monkeypatch):
        path = _write_secrets(
            'TELEGRAM_BOT_TOKEN=testtoken\n'
            'TELEGRAM_ALLOWED_USERS=12345\n'
            'TELEGRAM_FORUM_CHAT_ID=\n'
        )
        monkeypatch.setattr(
            "genesis.channels.bridge.secrets_path", lambda: path,
        )
        config = _load_bridge_config()
        assert config["forum_chat_id"] is None
        os.unlink(path)

    def test_forum_chat_id_positive(self, monkeypatch):
        path = _write_secrets(
            'TELEGRAM_BOT_TOKEN=testtoken\n'
            'TELEGRAM_ALLOWED_USERS=12345\n'
            'TELEGRAM_FORUM_CHAT_ID=999\n'
        )
        monkeypatch.setattr(
            "genesis.channels.bridge.secrets_path", lambda: path,
        )
        config = _load_bridge_config()
        assert config["forum_chat_id"] == 999
        os.unlink(path)
