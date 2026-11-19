"""Daily auth canary for the Guardian's recovery brain (guardian/cc_canary.py).

A stub `claude` on disk stands in for the real one, so nothing here makes a
real call. The canary must page once per failure episode with Claude Code's
own reason, page an all-clear on recovery, and run at most once per interval.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.guardian.cc_canary import maybe_run_cc_canary
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisEngine

_FAIL = json.dumps(
    {
        "type": "result",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "Failed to authenticate. API Error: 401 Invalid bearer token",
        "api_error_status": 401,
    }
)
_OK = json.dumps({"type": "result", "is_error": False, "result": "ok"})


def _stub(tmp_path, body: str, code: int):
    calls = tmp_path / "calls"
    stub = tmp_path / "claude"
    stub.write_text(
        f'#!/bin/sh\necho "$*" >> "{calls}"\ncat <<\'STUBEOF\'\n{body}\nSTUBEOF\nexit {code}\n'
    )
    stub.chmod(0o755)
    return stub, calls


def _setup(tmp_path, monkeypatch, body: str, code: int, **cc):
    stub, calls = _stub(tmp_path, body, code)
    config = GuardianConfig()
    config.state_dir = str(tmp_path / "state")
    config.cc.path = str(stub)
    config.cc.work_dir = str(tmp_path / "work")
    for k, v in cc.items():
        setattr(config.cc, k, v)
    config.state_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "genesis.guardian.diagnosis.DiagnosisEngine._resolve_cc_env",
        AsyncMock(return_value=None),
    )
    dispatcher = AsyncMock()
    dispatcher.send = AsyncMock(return_value=True)
    return config, DiagnosisEngine(config), dispatcher, calls


def _state(config) -> dict:
    return json.loads((config.state_path / "cc_canary.json").read_text())


@pytest.mark.asyncio
async def test_success_records_ok_and_pages_nothing(tmp_path, monkeypatch):
    config, engine, dispatcher, calls = _setup(tmp_path, monkeypatch, _OK, 0)
    await maybe_run_cc_canary(config, engine, dispatcher)
    assert _state(config)["ok"] is True
    dispatcher.send.assert_not_awaited()
    assert "-p" in calls.read_text()


@pytest.mark.asyncio
async def test_failure_pages_once_with_claude_codes_own_reason(tmp_path, monkeypatch):
    config, engine, dispatcher, _ = _setup(tmp_path, monkeypatch, _FAIL, 1)
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    await maybe_run_cc_canary(config, engine, dispatcher, now=now)
    (alert,) = [c.args[0] for c in dispatcher.send.await_args_list]
    assert alert.severity == "warning"
    assert "cannot authenticate" in alert.title
    assert "Failed to authenticate" in alert.body and "api_error_status 401" in alert.body
    assert _state(config)["alerted"] is True
    # The next due run that still fails does not page again.
    await maybe_run_cc_canary(config, engine, dispatcher, now=now + timedelta(hours=25))
    assert dispatcher.send.await_count == 1


@pytest.mark.asyncio
async def test_recovery_after_a_failure_pages_an_all_clear(tmp_path, monkeypatch):
    config, engine, dispatcher, _ = _setup(tmp_path, monkeypatch, _FAIL, 1)
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    await maybe_run_cc_canary(config, engine, dispatcher, now=now)
    _stub(tmp_path, _OK, 0)  # the brain can authenticate again
    await maybe_run_cc_canary(config, engine, dispatcher, now=now + timedelta(hours=25))
    severities = [c.args[0].severity for c in dispatcher.send.await_args_list]
    assert severities == ["warning", "info"]
    assert _state(config)["alerted"] is False


@pytest.mark.asyncio
async def test_runs_at_most_once_per_interval(tmp_path, monkeypatch):
    config, engine, dispatcher, calls = _setup(tmp_path, monkeypatch, _OK, 0)
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    await maybe_run_cc_canary(config, engine, dispatcher, now=now)
    await maybe_run_cc_canary(config, engine, dispatcher, now=now + timedelta(hours=23))
    assert len(calls.read_text().splitlines()) == 1
    await maybe_run_cc_canary(config, engine, dispatcher, now=now + timedelta(hours=24))
    assert len(calls.read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_disabled_canary_never_runs(tmp_path, monkeypatch):
    config, engine, dispatcher, calls = _setup(tmp_path, monkeypatch, _OK, 0, canary_enabled=False)
    await maybe_run_cc_canary(config, engine, dispatcher)
    assert not calls.exists()
    assert not (config.state_path / "cc_canary.json").exists()


@pytest.mark.asyncio
async def test_a_page_that_could_not_be_sent_is_tried_again_next_run(tmp_path, monkeypatch):
    config, engine, dispatcher, _ = _setup(tmp_path, monkeypatch, _FAIL, 1)
    dispatcher.send = AsyncMock(return_value=False)
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    await maybe_run_cc_canary(config, engine, dispatcher, now=now)
    assert _state(config)["alerted"] is False
    dispatcher.send = AsyncMock(return_value=True)
    await maybe_run_cc_canary(config, engine, dispatcher, now=now + timedelta(hours=25))
    assert dispatcher.send.await_count == 1


@pytest.mark.asyncio
async def test_the_canary_uses_the_diagnosis_credentials(tmp_path, monkeypatch):
    config, engine, dispatcher, _ = _setup(tmp_path, monkeypatch, _OK, 0)
    resolver = AsyncMock(
        return_value={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-SYNTHETIC", "PATH": "/usr/bin:/bin"}
    )
    monkeypatch.setattr("genesis.guardian.diagnosis.DiagnosisEngine._resolve_cc_env", resolver)
    await maybe_run_cc_canary(config, engine, dispatcher)
    resolver.assert_awaited_once()
    assert _state(config)["ok"] is True


@pytest.mark.asyncio
async def test_the_canary_runs_the_configured_model(tmp_path, monkeypatch):
    config, engine, dispatcher, calls = _setup(tmp_path, monkeypatch, _OK, 0, model="opus")
    await maybe_run_cc_canary(config, engine, dispatcher)
    assert "--model opus" in calls.read_text()


@pytest.mark.asyncio
async def test_an_unwritable_state_file_skips_the_run(tmp_path, monkeypatch):
    config, engine, dispatcher, calls = _setup(tmp_path, monkeypatch, _FAIL, 1)

    def _refuse(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr("genesis.guardian.cc_canary.atomic_write_text", _refuse)
    await maybe_run_cc_canary(config, engine, dispatcher)
    assert not calls.exists()  # never ran, so it cannot run (and page) every tick
    dispatcher.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_clock_that_went_backwards_does_not_silence_the_canary(tmp_path, monkeypatch):
    config, engine, dispatcher, calls = _setup(tmp_path, monkeypatch, _OK, 0)
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    (config.state_path / "cc_canary.json").write_text(
        json.dumps({"last_run_at": (now + timedelta(days=30)).isoformat(), "ok": True})
    )
    await maybe_run_cc_canary(config, engine, dispatcher, now=now)
    assert calls.exists()
