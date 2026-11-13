"""``python -m genesis.guardian --test-alert``: one alert, one JSON line, never queued."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.guardian import __main__ as guardian_main
from genesis.guardian.alert.dispatcher import AlertDispatcher


def _channel(result):
    ch = MagicMock()
    ch.send = (
        AsyncMock(side_effect=result)
        if isinstance(result, Exception)
        else AsyncMock(return_value=result)
    )
    return ch


@pytest.fixture
def run(monkeypatch, tmp_path):
    def _run(channels):
        dispatcher = AlertDispatcher(channels, fallback_queue_root=tmp_path / "queue")
        monkeypatch.setattr("genesis.guardian.config.load_config", lambda: MagicMock())
        monkeypatch.setattr("genesis.guardian.check._build_dispatcher", lambda _cfg: dispatcher)
        return guardian_main._test_alert_json

    return _run


async def test_sent_prints_one_ok_line(run, capsys, tmp_path):
    ch = _channel(True)
    rc = await run([ch])()
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert len(out) == 1
    assert json.loads(out[0]) == {"ok": True, "action": "test-alert", "sent": True, "error": None}
    alert = ch.send.await_args.args[0]
    assert alert.title == "Guardian test alert"


@pytest.mark.parametrize("result", [False, RuntimeError("boom")])
async def test_unconfirmed_send_is_not_ok_and_never_queued(run, capsys, tmp_path, result):
    rc = await run([_channel(result)])()
    out = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert len(out) == 1
    assert json.loads(out[0]) == {
        "ok": False,
        "action": "test-alert",
        "sent": False,
        "error": "send not confirmed",
    }
    assert not (tmp_path / "queue").exists()


async def test_no_channel_is_reported_and_never_queued(run, capsys, tmp_path):
    rc = await run([])()
    out = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert json.loads(out[0])["error"] == "no alert channel configured"
    assert not (tmp_path / "queue").exists()


def test_main_routes_the_flag_and_exits_with_its_code(monkeypatch):
    monkeypatch.setattr("sys.argv", ["guardian", "--test-alert"])
    monkeypatch.setattr("genesis.guardian.check._setup_logging", lambda: None)
    called = {}

    async def fake():
        called["yes"] = True
        return 1

    monkeypatch.setattr(guardian_main, "_test_alert_json", fake)
    monkeypatch.setattr(
        "genesis.guardian.check.run_check", AsyncMock(side_effect=AssertionError("ran a check"))
    )
    with pytest.raises(SystemExit) as exc:
        guardian_main.main()
    assert exc.value.code == 1
    assert called == {"yes": True}


async def test_a_config_error_still_prints_one_json_line(monkeypatch, capsys):
    def boom():
        raise ValueError("bad yaml")

    monkeypatch.setattr("genesis.guardian.config.load_config", boom)
    rc = await guardian_main._test_alert_json()
    out = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert len(out) == 1
    line = json.loads(out[0])
    assert line["ok"] is False and line["sent"] is False and "bad yaml" in line["error"]
