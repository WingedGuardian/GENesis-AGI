"""Tests for Telegram credential bridge — shared mount propagation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from genesis.guardian.credential_bridge import (
    load_telegram_credentials,
    propagate_telegram_credentials,
)


class TestPropagateTelegramCredentials:
    """Test container-side credential writer."""

    def _write_secrets(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_writes_only_telegram_keys(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "API_KEY_GROQ=groq-secret\n"
            "TELEGRAM_BOT_TOKEN=bot123\n"
            "TELEGRAM_FORUM_CHAT_ID=chat456\n"
            "API_KEY_MISTRAL=mistral-secret\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "bot123" in text
        assert "chat456" in text
        assert "groq-secret" not in text
        assert "mistral-secret" not in text

    def test_maps_forum_chat_id_to_chat_id(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=bot-token\n"
            "TELEGRAM_FORUM_CHAT_ID=-100999\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_CHAT_ID=-100999" in text
        # Should NOT contain the source key name
        assert "TELEGRAM_FORUM_CHAT_ID" not in text

    def test_accepts_direct_chat_id(self, tmp_path: Path) -> None:
        """If secrets.env uses TELEGRAM_CHAT_ID directly, it still works."""
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=bot-token\n"
            "TELEGRAM_CHAT_ID=-200888\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_CHAT_ID=-200888" in text

    def test_forum_chat_id_takes_priority(self, tmp_path: Path) -> None:
        """TELEGRAM_FORUM_CHAT_ID wins over TELEGRAM_CHAT_ID when both present."""
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=bot-token\n"
            "TELEGRAM_FORUM_CHAT_ID=-100first\n"
            "TELEGRAM_CHAT_ID=-200second\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_CHAT_ID=-100first" in text

    def test_chmod_600(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, "TELEGRAM_BOT_TOKEN=tok\n")
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        mode = stat.S_IMODE(os.stat(result).st_mode)
        assert mode == 0o600

    def test_returns_none_no_bot_token(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_FORUM_CHAT_ID=chat-only\n"
            "API_KEY_GROQ=something\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is None

    def test_returns_none_missing_secrets(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(
            shared_dir=shared,
            secrets_path=tmp_path / "nonexistent.env",
        )
        assert result is None

    def test_creates_directory(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, "TELEGRAM_BOT_TOKEN=tok\n")
        shared = tmp_path / "deep" / "nested" / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        assert result.parent.exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=tok\n"
            "TELEGRAM_FORUM_CHAT_ID=chat\n"
        ))
        shared = tmp_path / "shared"
        result1 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        result2 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result1 is not None
        assert result2 is not None
        assert result1.read_text() == result2.read_text()

    def test_includes_thread_id(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN=tok\n"
            "TELEGRAM_THREAD_ID=42\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "TELEGRAM_THREAD_ID=42" in text

    def test_handles_quoted_values(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "TELEGRAM_BOT_TOKEN='quoted-tok'\n"
            "TELEGRAM_FORUM_CHAT_ID=\"double-quoted\"\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "quoted-tok" in text
        assert "double-quoted" in text

    def test_handles_export_prefix(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, (
            "export TELEGRAM_BOT_TOKEN=export-tok\n"
            "export TELEGRAM_FORUM_CHAT_ID=export-chat\n"
        ))
        shared = tmp_path / "shared"
        result = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result is not None
        text = result.read_text()
        assert "export-tok" in text
        assert "export-chat" in text

    def test_skips_write_when_unchanged(self, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets.env"
        self._write_secrets(secrets, "TELEGRAM_BOT_TOKEN=tok\n")
        shared = tmp_path / "shared"
        result1 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result1 is not None
        mtime1 = result1.stat().st_mtime
        # Second call should skip write (content unchanged)
        import time
        time.sleep(0.01)  # Ensure mtime would differ if rewritten
        result2 = propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)
        assert result2 is not None
        mtime2 = result2.stat().st_mtime
        assert mtime1 == mtime2


class TestLoadTelegramCredentials:
    """Test host-side credential reader."""

    def test_reads_valid_file(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "shared" / "guardian"
        creds_dir.mkdir(parents=True)
        (creds_dir / "telegram_creds.env").write_text(
            "TELEGRAM_BOT_TOKEN=bot123\n"
            "TELEGRAM_CHAT_ID=chat456\n"
        )
        result = load_telegram_credentials(str(tmp_path))
        assert result["TELEGRAM_BOT_TOKEN"] == "bot123"
        assert result["TELEGRAM_CHAT_ID"] == "chat456"

    def test_returns_empty_for_missing(self, tmp_path: Path) -> None:
        result = load_telegram_credentials(str(tmp_path / "nonexistent"))
        assert result == {}

    def test_handles_comments_and_blanks(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "shared" / "guardian"
        creds_dir.mkdir(parents=True)
        (creds_dir / "telegram_creds.env").write_text(
            "# This is a comment\n"
            "\n"
            "TELEGRAM_BOT_TOKEN=tok\n"
            "  \n"
        )
        result = load_telegram_credentials(str(tmp_path))
        assert result["TELEGRAM_BOT_TOKEN"] == "tok"
        assert len(result) == 1


class TestRoundTrip:
    """Test write → read round trip."""

    def test_propagate_then_load(self, tmp_path: Path) -> None:
        # Container side: write to shared_dir (= {state_dir}/shared on host)
        secrets = tmp_path / "container" / "secrets.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text(
            "TELEGRAM_BOT_TOKEN=my-bot-token\n"
            "TELEGRAM_FORUM_CHAT_ID=-100group\n"
            "TELEGRAM_THREAD_ID=7\n"
            "API_KEY_GROQ=should-not-appear\n"
        )
        # Simulate the real mount: writer sees shared_dir, reader sees state_dir/shared
        shared = tmp_path / "state" / "shared"
        propagate_telegram_credentials(shared_dir=shared, secrets_path=secrets)

        # Host side: load_telegram_credentials takes state_dir, adds shared/guardian/
        result = load_telegram_credentials(str(tmp_path / "state"))
        assert result["TELEGRAM_BOT_TOKEN"] == "my-bot-token"
        assert result["TELEGRAM_CHAT_ID"] == "-100group"
        assert result["TELEGRAM_THREAD_ID"] == "7"
        assert "API_KEY_GROQ" not in result
        assert "TELEGRAM_FORUM_CHAT_ID" not in result
