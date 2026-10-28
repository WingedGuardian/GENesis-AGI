"""Tests for the container-side ingest of the Guardian's sent-alert log.

The Guardian's alerts must land in telegram_messages as sender="guardian"
outbound rows, idempotently, from a cursor derived from rows already stored.
An old Guardian, a broken response or a malformed entry must be a quiet no-op,
and every entry is validated as untrusted data before it is stored.
Chat ids are synthetic.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.remote import GuardianRemote
from genesis.guardian.sent_log_ingest import ingest_guardian_sent_log

_CHAT = -1000000000001  # synthetic


def _entry(message_id: int, ts: float = 1761600000.5, **over) -> dict:
    e = {
        "ts": ts,
        "chat_id": _CHAT,
        "thread_id": 7,
        "message_id": message_id,
        "text": f"<b>Guardian: alert {message_id}</b>",
        "kind": "alert",
    }
    e.update(over)
    return e


def _response(*entries, count: int | None = None, raw_lines=()) -> str:
    lines = [json.dumps(e) for e in entries] + list(raw_lines)
    n = len(lines) if count is None else count
    lines.append(json.dumps({"ok": True, "action": "sent-log", "count": n}))
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _reset_drift_flag(monkeypatch):
    monkeypatch.setattr("genesis.guardian.sent_log_ingest._drift_warned", False)


class FakeRemote:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.since: list[float] = []

    async def sent_log(self, since: float):
        self.since.append(since)
        return self.responses.pop(0)


async def _rows(db) -> list[dict]:
    cur = await db.execute(
        "SELECT chat_id, message_id, thread_id, sender, content, timestamp, direction "
        "FROM telegram_messages ORDER BY message_id",
    )
    return [dict(r) for r in await cur.fetchall()]


class TestIngest:
    @pytest.mark.asyncio
    async def test_stores_entries_as_guardian_outbound(self, db) -> None:
        remote = FakeRemote((True, _response(_entry(11), _entry(12, ts=1761600001.0))))
        assert await ingest_guardian_sent_log(db, remote=remote) == 2
        rows = await _rows(db)
        assert rows[0] == {
            "chat_id": _CHAT,
            "message_id": 11,
            "thread_id": 7,
            "sender": "guardian",
            "content": "<b>Guardian: alert 11</b>",
            "timestamp": "2025-10-27T21:20:00.500000+00:00",
            "direction": "outbound",
        }
        assert [r["message_id"] for r in rows] == [11, 12]
        assert remote.since == [0.0]  # zero state: no guardian rows yet

    @pytest.mark.asyncio
    async def test_cursor_and_idempotency(self, db) -> None:
        first = _response(_entry(11, ts=1761600000.5), _entry(12, ts=1761600002.25))
        # The host bound is inclusive, so the next pull repeats entry 12.
        second = _response(_entry(12, ts=1761600002.25), _entry(13, ts=1761600003.0))
        remote = FakeRemote((True, first), (True, second))
        assert await ingest_guardian_sent_log(db, remote=remote) == 2
        # Only the genuinely new row counts; the repeat is skipped, not re-offered.
        assert await ingest_guardian_sent_log(db, remote=remote) == 1
        assert remote.since == [0.0, 1761600002.25]
        assert [r["message_id"] for r in await _rows(db)] == [11, 12, 13]

    @pytest.mark.asyncio
    async def test_cursor_ignores_genesis_rows(self, db) -> None:
        from genesis.db.crud import telegram_messages

        await telegram_messages.store(
            db,
            chat_id=_CHAT,
            message_id=1,
            sender="genesis",
            content="hi",
            timestamp="2026-01-01T00:00:00+00:00",
            direction="outbound",
        )
        remote = FakeRemote((True, _response()))
        await ingest_guardian_sent_log(db, remote=remote)
        assert remote.since == [0.0]

    @pytest.mark.asyncio
    async def test_future_row_does_not_pin_the_cursor(self, db) -> None:
        """A row stored while the clock ran ahead (then stepped back) must not
        hide every later alert: the cursor ignores rows stamped after now."""
        from genesis.db.crud import telegram_messages

        await telegram_messages.store(
            db, chat_id=_CHAT, message_id=99, sender="guardian", content="ahead",
            timestamp="2255-01-01T00:00:00.000000+00:00", direction="outbound",
        )
        remote = FakeRemote((True, _response(_entry(11, ts=1761600000.5))), (True, _response()))
        await ingest_guardian_sent_log(db, remote=remote)
        await ingest_guardian_sent_log(db, remote=remote)
        assert remote.since == [0.0, 1761600000.5]

    @pytest.mark.asyncio
    async def test_future_entry_is_refused(self, db) -> None:
        """A future-stamped row would sort as the newest message of its chat in
        every recap; beyond a small skew it is not stored."""
        import time

        reply = _response(_entry(11), _entry(12, ts=time.time() + 3600))
        assert await ingest_guardian_sent_log(db, remote=FakeRemote((True, reply))) == 1
        assert [r["message_id"] for r in await _rows(db)] == [11]

    @pytest.mark.asyncio
    async def test_unencodable_text_skipped_and_page_still_lands(self, db) -> None:
        """A lone surrogate passes every str check but fails the SQLite bind; it
        must not abort the page (or wedge the cursor on every tick)."""
        bad = json.dumps(_entry(12)).replace("alert 12", "alert \\ud800")
        lines = [json.dumps(_entry(11)), bad, json.dumps(_entry(13, ts=1761600009.0))]
        lines.append(json.dumps({"ok": True, "action": "sent-log", "count": 3}))
        reply = (True, "\n".join(lines))
        assert await ingest_guardian_sent_log(db, remote=FakeRemote(reply)) == 2
        assert [r["message_id"] for r in await _rows(db)] == [11, 13]

    @pytest.mark.asyncio
    async def test_store_failure_skips_only_that_row(self, db) -> None:
        from genesis.db.crud import telegram_messages

        real = telegram_messages.store

        async def flaky(db_, **kw):
            if kw["message_id"] == 12:
                raise RuntimeError("boom")
            await real(db_, **kw)

        reply = (True, _response(_entry(11), _entry(12), _entry(13)))
        with patch.object(telegram_messages, "store", side_effect=flaky):
            assert await ingest_guardian_sent_log(db, remote=FakeRemote(reply)) == 2
        assert [r["message_id"] for r in await _rows(db)] == [11, 13]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            (False, '{"ok": false, "error": "denied"}'),  # gateway predates the verb
            (
                False,
                '{"ok": false, "action": "sent-log", "error": "guardian src predates sent-log"}',
            ),
            (False, "timeout"),
        ],
    )
    async def test_old_or_broken_guardian_is_a_quiet_noop(self, db, caplog, reply) -> None:
        caplog.set_level(logging.DEBUG, logger="genesis.guardian.sent_log_ingest")
        assert await ingest_guardian_sent_log(db, remote=FakeRemote(reply)) == 0
        assert await _rows(db) == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            "",  # no terminal line at all
            "2026-10-28T00:00:00 [guardian] INFO something\n",  # stderr fallback
        ],
    )
    async def test_off_protocol_warns_once_then_quiet(self, db, caplog, body) -> None:
        caplog.set_level(logging.DEBUG, logger="genesis.guardian.sent_log_ingest")
        remote = FakeRemote((True, body), (True, body), (True, body))
        for _ in range(3):
            assert await ingest_guardian_sent_log(db, remote=remote) == 0
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert await _rows(db) == []

    @pytest.mark.asyncio
    async def test_incomplete_response_stores_nothing(self, db) -> None:
        # Valid entries but the terminal count does not match: a truncated read.
        reply = (True, _response(_entry(11), _entry(12), count=3))
        assert await ingest_guardian_sent_log(db, remote=FakeRemote(reply)) == 0
        assert await _rows(db) == []

    @pytest.mark.asyncio
    async def test_terminal_line_must_be_ok(self, db) -> None:
        body = (
            json.dumps(_entry(11))
            + "\n"
            + json.dumps(
                {"ok": False, "action": "sent-log", "count": 1},
            )
        )
        assert await ingest_guardian_sent_log(db, remote=FakeRemote((True, body))) == 0
        assert await _rows(db) == []

    @pytest.mark.asyncio
    async def test_malformed_entries_are_skipped(self, db) -> None:
        bad = [
            _entry(20, chat_id=True),
            _entry(21, chat_id="123"),
            _entry(22, chat_id=0),
            _entry(23, thread_id="7"),
            {**_entry(24), "message_id": 0},
            {**_entry(25), "message_id": 2**63},
            _entry(26, kind="prompt"),
            _entry(27, text=""),
            _entry(28, text="   "),
            _entry(29, text="x" * 32_769),
            _entry(30, text=["list"]),
            _entry(31, ts=-1),
            _entry(32, ts=1e10),
            _entry(34, ts=1e300),
            _entry(33, ts="1761600000"),
            ["not", "a", "dict"],
        ]
        reply = (True, _response(_entry(11), *bad, _entry(12), raw_lines=["{not json", "NaN"]))
        assert await ingest_guardian_sent_log(db, remote=FakeRemote(reply)) == 2
        assert [r["message_id"] for r in await _rows(db)] == [11, 12]

    @pytest.mark.asyncio
    async def test_nan_ts_is_skipped(self, db) -> None:
        body = '{"ts": NaN, "chat_id": -1, "thread_id": null, "message_id": 5, "text": "t", "kind": "alert"}'
        body += "\n" + json.dumps({"ok": True, "action": "sent-log", "count": 1})
        assert await ingest_guardian_sent_log(db, remote=FakeRemote((True, body))) == 0

    @pytest.mark.asyncio
    async def test_oversized_response_is_refused_whole(self, db) -> None:
        lines = [_entry(i) for i in range(1, 202)]  # more than the host ever returns
        assert await ingest_guardian_sent_log(db, remote=FakeRemote((True, _response(*lines)))) == 0
        assert await _rows(db) == []

    @pytest.mark.asyncio
    async def test_remote_raising_never_escapes(self, db) -> None:
        remote = MagicMock()
        remote.sent_log = AsyncMock(side_effect=RuntimeError("boom"))
        assert await ingest_guardian_sent_log(db, remote=remote) == 0

    @pytest.mark.asyncio
    async def test_kill_switch(self, db, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_GUARDIAN_SENT_LOG_DISABLED", "1")
        remote = FakeRemote()
        assert await ingest_guardian_sent_log(db, remote=remote) == 0
        assert remote.since == []


# Mirrors the gateway's shape check for `sent-log --since <epoch>`.
_GATEWAY_SINCE_RE = re.compile(r"[0-9]{1,10}(\.[0-9]{1,6})?")


class TestRemoteSentLog:
    @pytest.mark.asyncio
    # 1e-7 and a 7-digit fraction are where a plain str(float) leaves the
    # shape the gateway admits (exponent; too many fraction digits).
    @pytest.mark.parametrize(
        "since",
        [0.0, -0.0, 1761600002.25, 9999999999.0, 1e-7, 1761600002.1234567],
    )
    async def test_formats_since_as_the_gateway_admits(self, since) -> None:
        remote = GuardianRemote(host_ip="192.0.2.10", host_user="guardian")
        with patch.object(remote, "_ssh_command", new=AsyncMock(return_value=(True, "x"))) as ssh:
            assert await remote.sent_log(since) == (True, "x")
        cmd = ssh.call_args.args[0]
        verb, flag, value = cmd.split(" ")
        assert (verb, flag) == ("sent-log", "--since")
        assert _GATEWAY_SINCE_RE.fullmatch(value)
        assert float(value) == pytest.approx(since, abs=1e-6)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("since", [-1.0, float("nan"), float("inf"), 1e10])
    async def test_out_of_range_never_reaches_ssh(self, since) -> None:
        remote = GuardianRemote(host_ip="192.0.2.10", host_user="guardian")
        with patch.object(remote, "_ssh_command", new=AsyncMock()) as ssh:
            ok, _ = await remote.sent_log(since)
        assert ok is False
        ssh.assert_not_called()


class TestWiring:
    @pytest.mark.asyncio
    async def test_init_wires_the_ingest_with_the_remote(self, tmp_path: Path) -> None:
        from genesis.runtime.init.guardian import init_guardian_monitoring

        (tmp_path / ".genesis").mkdir()
        (tmp_path / ".genesis" / "guardian_remote.yaml").write_text(
            "host_ip: 192.0.2.10\nhost_user: guardian\n",
        )
        rt = MagicMock()
        with patch("genesis.runtime.init.guardian.Path.home", return_value=tmp_path):
            await init_guardian_monitoring(rt)
        rt._awareness_loop.set_guardian_sent_log_ingest.assert_called_once()
        fn = rt._awareness_loop.set_guardian_sent_log_ingest.call_args.args[0]
        assert fn.func is ingest_guardian_sent_log
        assert fn.keywords == {"remote": rt._guardian_remote}

    @pytest.mark.asyncio
    async def test_tick_runs_the_injected_ingest(self, db, monkeypatch) -> None:
        """A real awareness tick must await the injected fn with its db."""
        from genesis.awareness.loop import AwarenessLoop

        def _fake_tracked_task(coro, *, name="", **kw):
            coro.close()

        monkeypatch.setattr("genesis.util.tasks.tracked_task", _fake_tracked_task)
        event_bus = MagicMock()
        event_bus.emit = AsyncMock()
        loop = AwarenessLoop(db=db, collectors=[], event_bus=event_bus)
        fn = AsyncMock(return_value=0)
        loop.set_guardian_sent_log_ingest(fn)
        await loop._on_tick()
        fn.assert_awaited_once_with(db)
