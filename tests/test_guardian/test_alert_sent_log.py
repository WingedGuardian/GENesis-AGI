"""Tests for the Guardian sent-alert log (alert/sent_log.py) and its writers/readers.

Delivered alerts must be recorded so the container can store them in its message
history; recording must never affect sending; the log must stay bounded; the
reader must hand back only well-formed entries. Chat ids are synthetic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from genesis.guardian import __main__ as guardian_main
from genesis.guardian.alert import sent_log
from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.telegram import TelegramAlertChannel
from genesis.guardian.check import _build_dispatcher
from genesis.guardian.config import GuardianConfig

_CHAT = -1000000000001  # synthetic


def _append(path: Path, message_id: int, text: str = "alert body") -> bool:
    return sent_log.append_sent(
        path,
        chat_id=_CHAT,
        thread_id=7,
        message_id=message_id,
        text=text,
    )


class TestAppendAndRead:
    def test_round_trip_oldest_first_and_inclusive(self, tmp_path: Path) -> None:
        log = tmp_path / "alerts" / "sent_log.jsonl"
        with patch.object(sent_log.time, "time", side_effect=[0.0, 100.0, 0.0, 200.0, 0.0, 300.0]):
            assert _append(log, 3)
            assert _append(log, 1)
            assert _append(log, 2)
        got = sent_log.read_since(log, 200.0, now=400.0)
        assert [e["message_id"] for e in got] == [1, 2]  # ts >= 200, oldest first
        assert got[0] == {
            "ts": 200.0,
            "chat_id": _CHAT,
            "thread_id": 7,
            "message_id": 1,
            "text": "alert body",
            "kind": "alert",
        }

    def test_limit_returns_the_oldest(self, tmp_path: Path) -> None:
        log = tmp_path / "sent_log.jsonl"
        for i in range(1, 6):
            _append(log, i)
        got = sent_log.read_since(log, 0, limit=2)
        assert [e["message_id"] for e in got] == [1, 2]

    def test_file_is_owner_only(self, tmp_path: Path) -> None:
        log = tmp_path / "sent_log.jsonl"
        _append(log, 1)
        assert (log.stat().st_mode & 0o777) == 0o600

    def test_age_bound_drops_old_entries(self, tmp_path: Path) -> None:
        log = tmp_path / "sent_log.jsonl"
        now = 10_000_000.0
        with patch.object(sent_log.time, "time", side_effect=[now, now - sent_log.MAX_AGE_S - 1, now, now]):
            _append(log, 1)
            _append(log, 2)
        assert [e["message_id"] for e in sent_log.read_since(log, 0, now=now)] == [2]

    def test_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "sent_log.jsonl"
        _append(log, 1)
        now = time.time()  # inside the age bound, so only validation can drop them
        bad = [
            {"chat_id": "x"},
            {"kind": "prompt"},
            {"chat_id": True},
            {"chat_id": 0},
            {"thread_id": "7"},
            {"thread_id": 0},
            {"message_id": 2**63},
            {"text": "   "},
            {"text": "x" * (sent_log.MAX_TEXT_CHARS + 1)},
            {"ts": 1e10},
        ]
        with log.open("ab") as fh:
            fh.write(b"not json\n")
            for i, over in enumerate(bad, start=10):
                entry = {"ts": now, "chat_id": _CHAT, "thread_id": None,
                         "message_id": i, "text": "t", "kind": "alert", **over}
                fh.write(json.dumps(entry).encode() + b"\n")
            fh.write(b"\xff\xfe\n")
        _append(log, 4)
        assert [e["message_id"] for e in sent_log.read_since(log, 0)] == [1, 4]

    def test_rotation_bounds_size_and_keeps_both_generations(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log = tmp_path / "sent_log.jsonl"
        monkeypatch.setattr(sent_log, "MAX_BYTES", 400)
        for i in range(1, 11):
            assert _append(log, i, text="x" * 60)
        older = log.with_name(log.name + ".1")
        assert older.exists()
        assert log.stat().st_size <= 400
        assert older.stat().st_size <= 400
        got = [e["message_id"] for e in sent_log.read_since(log, 0)]
        # Only the two newest generations survive; what survives is contiguous
        # and ends at the newest entry.
        assert got == list(range(got[0], 11))
        assert len(got) < 10

    def test_append_never_raises(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("")
        # Parent "directory" is a regular file: mkdir fails inside append_sent.
        assert _append(blocker / "sent_log.jsonl", 1) is False

    def test_append_rejects_bad_entry(self, tmp_path: Path) -> None:
        log = tmp_path / "sent_log.jsonl"
        assert (
            sent_log.append_sent(
                log,
                chat_id=_CHAT,
                thread_id=None,
                message_id=0,
                text="t",
            )
            is False
        )
        assert not log.exists()


class TestWriteRobustness:
    def test_torn_last_line_does_not_swallow_the_next_entry(self, tmp_path: Path) -> None:
        log = tmp_path / "sent_log.jsonl"
        _append(log, 1)
        with log.open("ab") as fh:
            fh.write(b'{"ts": 1, "chat_id"')  # a short write, no newline
        _append(log, 2)
        assert [e["message_id"] for e in sent_log.read_since(log, 0)] == [1, 2]

    def test_busy_lock_gives_up_without_blocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import fcntl
        import os

        log = tmp_path / "sent_log.jsonl"
        monkeypatch.setattr(sent_log, "_LOCK_WAIT_S", 0.2)
        holder = os.open(tmp_path / "sent_log.jsonl.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            started = time.monotonic()
            assert _append(log, 1) is False
            assert time.monotonic() - started < 5
        finally:
            os.close(holder)
        assert _append(log, 2) is True


class TestTelegramRecordsAlerts:
    def _channel(self, tmp_path: Path) -> TelegramAlertChannel:
        return TelegramAlertChannel(
            bot_token="test-token",
            chat_id=str(_CHAT),
            thread_id="7",
            sent_log_path=tmp_path / "sent_log.jsonl",
        )

    @pytest.mark.asyncio
    async def test_delivered_alert_is_recorded(self, tmp_path: Path) -> None:
        ch = self._channel(tmp_path)
        alert = Alert(severity=AlertSeverity.WARNING, title="Disk", body="Pool at 91%")
        with patch.object(ch, "_send_message", return_value=4242):
            assert await ch.send(alert) is True
        got = sent_log.read_since(tmp_path / "sent_log.jsonl", 0)
        assert len(got) == 1
        assert got[0]["message_id"] == 4242
        assert got[0]["chat_id"] == _CHAT
        assert got[0]["thread_id"] == 7
        assert got[0]["kind"] == "alert"
        assert got[0]["text"] == ch._format_alert(alert)

    @pytest.mark.asyncio
    async def test_failed_send_is_not_recorded(self, tmp_path: Path) -> None:
        ch = self._channel(tmp_path)
        with patch.object(ch, "_send_message", return_value=None):
            assert await ch.send(Alert(severity=AlertSeverity.INFO, title="t", body="b")) is False
        assert not (tmp_path / "sent_log.jsonl").exists()

    @pytest.mark.asyncio
    async def test_approval_prompt_is_not_recorded(self, tmp_path: Path) -> None:
        ch = self._channel(tmp_path)
        with patch.object(ch, "_send_message", return_value=99):
            assert await ch.send_text("Reply APPROVE") == 99
        assert not (tmp_path / "sent_log.jsonl").exists()

    @pytest.mark.asyncio
    async def test_recording_failure_still_reports_delivered(self, tmp_path: Path) -> None:
        ch = self._channel(tmp_path)
        with (
            patch.object(ch, "_send_message", return_value=5),
            patch.object(sent_log, "append_sent", side_effect=RuntimeError("boom")),
        ):
            assert await ch.send(Alert(severity=AlertSeverity.INFO, title="t", body="b")) is True

    @pytest.mark.asyncio
    async def test_non_numeric_chat_id_is_skipped_and_warned_once(
        self, tmp_path: Path, caplog,
    ) -> None:
        ch = TelegramAlertChannel(
            bot_token="test-token", chat_id="@example_channel",
            sent_log_path=tmp_path / "sent_log.jsonl",
        )
        alert = Alert(severity=AlertSeverity.INFO, title="t", body="b")
        with patch.object(ch, "_send_message", return_value=5):
            assert await ch.send(alert) is True
            assert await ch.send(alert) is True
        assert not (tmp_path / "sent_log.jsonl").exists()
        skips = [r for r in caplog.records if "not numeric" in r.getMessage()]
        assert len(skips) == 1

    @pytest.mark.asyncio
    async def test_record_raising_still_reports_delivered(self, tmp_path: Path) -> None:
        ch = self._channel(tmp_path)
        with (
            patch.object(ch, "_send_message", return_value=5),
            patch.object(ch, "_record_sent", side_effect=RuntimeError("boom")),
        ):
            assert await ch.send(Alert(severity=AlertSeverity.INFO, title="t", body="b")) is True

    def test_dispatcher_wires_log_under_state_dir(self, tmp_path: Path) -> None:
        config = GuardianConfig()
        config.state_dir = str(tmp_path)
        config.alert.telegram_bot_token = "test-token"
        config.alert.telegram_chat_id = str(_CHAT)
        (channel,) = _build_dispatcher(config)._channels
        assert channel._sent_log_path == tmp_path / "alerts" / "sent_log.jsonl"


class TestSentLogCli:
    def _config(self, tmp_path: Path) -> GuardianConfig:
        config = GuardianConfig()
        config.state_dir = str(tmp_path)
        return config

    def test_prints_entries_then_terminal_line(self, tmp_path: Path, capsys) -> None:
        log = tmp_path / "alerts" / "sent_log.jsonl"
        _append(log, 1)
        _append(log, 2)
        with patch("genesis.guardian.config.load_config", return_value=self._config(tmp_path)):
            assert guardian_main._sent_log(["x", "--sent-log", "0"]) == 0
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [e["message_id"] for e in lines[:-1]] == [1, 2]
        assert lines[-1] == {"ok": True, "action": "sent-log", "count": 2}

    def test_empty_log_still_prints_terminal_line(self, tmp_path: Path, capsys) -> None:
        with patch("genesis.guardian.config.load_config", return_value=self._config(tmp_path)):
            assert guardian_main._sent_log(["x", "--sent-log", str(time.time())]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "ok": True,
            "action": "sent-log",
            "count": 0,
        }

    @pytest.mark.parametrize("bad", [[], ["nan"], ["inf"], ["-1"], ["abc"]])
    def test_invalid_since_is_refused(self, tmp_path: Path, capsys, bad) -> None:
        with patch("genesis.guardian.config.load_config", return_value=self._config(tmp_path)):
            assert guardian_main._sent_log(["x", "--sent-log", *bad]) == 1
        assert json.loads(capsys.readouterr().out)["ok"] is False
