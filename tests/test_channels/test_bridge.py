"""Tests for bridge config loading."""

import os
import tempfile

import pytest

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


class TestYieldToServer:
    """The bridge must refuse to run alongside genesis-server (dual-runtime /
    dual-getUpdates guard). Server not running → guard is a no-op (legacy
    fallback preserved)."""

    def test_exits_200_when_server_lock_held(self, tmp_path, monkeypatch):
        import subprocess
        import sys as _sys
        import textwrap
        import time

        import pytest

        from genesis.channels.bridge import _yield_to_server
        from genesis.util.process_lock import EXIT_ALREADY_RUNNING

        holder = subprocess.Popen(
            [
                _sys.executable,
                "-c",
                textwrap.dedent(f"""\
                    import time
                    from pathlib import Path
                    from genesis.util.process_lock import ProcessLock
                    with ProcessLock("genesis-server", pid_dir=Path("{tmp_path}")):
                        time.sleep(30)
                """),
            ],
        )
        try:
            lock_path = tmp_path / "genesis-server.lock"
            for _ in range(100):
                if lock_path.exists() and lock_path.read_text().strip():
                    break
                time.sleep(0.1)
            with pytest.raises(SystemExit) as exc:
                _yield_to_server(pid_dir=tmp_path)
            assert exc.value.code == EXIT_ALREADY_RUNNING
        finally:
            holder.terminate()
            holder.wait(timeout=10)

    def test_noop_when_server_not_running(self, tmp_path):
        from genesis.channels.bridge import _yield_to_server

        _yield_to_server(pid_dir=tmp_path)  # must not raise


class TestLateYieldCheck:
    """The post-bootstrap re-probe: a server that started during the bridge's
    ~90s bootstrap is detected before polling OR headless continuation."""

    @pytest.mark.asyncio
    async def test_shuts_down_and_exits_200_when_server_appeared(self, tmp_path):
        import subprocess
        import sys as _sys
        import textwrap
        import time
        from unittest.mock import AsyncMock

        import pytest

        from genesis.channels.bridge import _late_yield_check
        from genesis.util.process_lock import EXIT_ALREADY_RUNNING

        holder = subprocess.Popen(
            [
                _sys.executable,
                "-c",
                textwrap.dedent(f"""\
                    import time
                    from pathlib import Path
                    from genesis.util.process_lock import ProcessLock
                    with ProcessLock("genesis-server", pid_dir=Path("{tmp_path}")):
                        time.sleep(30)
                """),
            ],
        )
        try:
            lock_path = tmp_path / "genesis-server.lock"
            for _ in range(100):
                if lock_path.exists() and lock_path.read_text().strip():
                    break
                time.sleep(0.1)
            runtime = AsyncMock()
            with pytest.raises(SystemExit) as exc:
                await _late_yield_check(runtime, pid_dir=tmp_path)
            assert exc.value.code == EXIT_ALREADY_RUNNING
            runtime.shutdown.assert_awaited_once()
        finally:
            holder.terminate()
            holder.wait(timeout=10)

    @pytest.mark.asyncio
    async def test_noop_when_server_absent(self, tmp_path):
        from unittest.mock import AsyncMock

        from genesis.channels.bridge import _late_yield_check

        runtime = AsyncMock()
        await _late_yield_check(runtime, pid_dir=tmp_path)
        runtime.shutdown.assert_not_awaited()
