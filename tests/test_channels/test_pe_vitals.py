"""Tests for Voice PE hardware-vitals polling (``channels.voice.pe_vitals``).

The poller reads the Voice PE's ESPHome sensors from Home Assistant's REST API.
It must NEVER raise: not-configured or HA-unreachable returns
``{"reachable": False, "reason": ...}`` so the dashboard degrades gracefully.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from genesis.channels.voice import pe_vitals

# A synthetic prefix — never the real install-specific device id.
_PREFIX = "home_assistant_test_voice_pe_"
_CLIENT = "genesis.channels.voice.pe_vitals.httpx.AsyncClient"


def _resp(status_code: int, payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=payload)
    r.raise_for_status = MagicMock()
    return r


def _state(state: str, unit: str | None = None) -> dict:
    attrs = {"unit_of_measurement": unit} if unit else {}
    return {"state": state, "attributes": attrs}


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("HA_URL", "http://ha.test:8123")
    monkeypatch.setenv("HA_LONG_LIVED_TOKEN", "tok")
    monkeypatch.setenv("HA_VOICE_PE_PREFIX", _PREFIX)


def _client_returning(get_fn) -> MagicMock:
    """Build a patched AsyncClient whose ``.get`` is driven by ``get_fn(url, headers=…)``."""
    mc = MagicMock()
    mc.return_value.__aenter__ = AsyncMock(
        return_value=MagicMock(get=AsyncMock(side_effect=get_fn)),
    )
    mc.return_value.__aexit__ = AsyncMock(return_value=False)
    return mc


def test_not_configured_is_graceful(monkeypatch):
    for var in ("HA_URL", "HA_LONG_LIVED_TOKEN", "HA_VOICE_PE_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())
    assert out["reachable"] is False
    assert "not configured" in out["reason"].lower()


def test_happy_path_parses_all_vitals(monkeypatch):
    _configure(monkeypatch)

    table = {
        f"sensor.{_PREFIX}internal_temperature": _state("111.2", "°F"),
        f"sensor.{_PREFIX}wifi_signal": _state("-37.0", "dBm"),
        f"sensor.{_PREFIX}uptime": _state("80282.0", "s"),
        f"sensor.{_PREFIX}reset_reason": _state("Reboot request from api"),
        f"sensor.{_PREFIX}heap_free": _state("65880.0", "B"),
        f"sensor.{_PREFIX}loop_time": _state("18.0", "ms"),
        f"binary_sensor.{_PREFIX}voice_assistant_connected": _state("off"),
        f"sensor.{_PREFIX}voice_assistant_status": _state("Idle"),
    }

    def fake_get(url, headers=None):
        return _resp(200, table[url.rsplit("/", 1)[-1]])

    with patch(_CLIENT, _client_returning(fake_get)):
        out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())

    assert out["reachable"] is True
    assert out["temperature"] == "111.2"
    assert out["temperature_unit"] == "°F"
    assert out["wifi_signal"] == "-37.0"
    assert out["reset_reason"] == "Reboot request from api"
    assert out["connected"] == "off"
    assert out["status"] == "Idle"


def test_ha_unreachable_is_graceful(monkeypatch):
    _configure(monkeypatch)

    def fake_get(url, headers=None):
        raise httpx.ConnectError("connection refused")

    with patch(_CLIENT, _client_returning(fake_get)):
        out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())

    assert out["reachable"] is False
    assert "reach" in out["reason"].lower()  # transport error → "could not reach Home Assistant"


def test_missing_entity_is_none_not_fatal(monkeypatch):
    _configure(monkeypatch)

    def fake_get(url, headers=None):
        if url.endswith("loop_time"):
            return _resp(404, {})
        return _resp(200, _state("42"))

    with patch(_CLIENT, _client_returning(fake_get)):
        out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())

    assert out["reachable"] is True
    assert out["loop_time"] is None
    assert out["temperature"] == "42"


def _resp_http_error(code: int) -> MagicMock:
    """A response whose ``raise_for_status()`` raises — HA replied with an error."""
    req = httpx.Request("GET", "http://ha.test/api/states/x")
    resp = httpx.Response(code, request=req)
    r = MagicMock()
    r.status_code = code
    r.json = MagicMock(return_value={})
    r.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("err", request=req, response=resp),
    )
    return r


def test_all_http_errors_reads_as_reachable_but_erroring(monkeypatch):
    """HA up but every entity 500s → reachable False, reason says HA WAS reachable."""
    _configure(monkeypatch)

    def fake_get(url, headers=None):
        return _resp_http_error(500)

    with patch(_CLIENT, _client_returning(fake_get)):
        out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())

    assert out["reachable"] is False
    assert "reachable" in out["reason"].lower()  # distinct from "could not reach"


def test_malformed_json_is_field_level_not_fatal(monkeypatch):
    _configure(monkeypatch)

    def fake_get(url, headers=None):
        if url.endswith("wifi_signal"):
            r = _resp(200, {})
            r.json = MagicMock(side_effect=json.JSONDecodeError("bad", "doc", 0))
            return r
        return _resp(200, _state("7"))

    with patch(_CLIENT, _client_returning(fake_get)):
        out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())

    assert out["reachable"] is True
    assert out["wifi_signal"] is None
    assert out["temperature"] == "7"


def test_all_entities_absent_is_graceful(monkeypatch):
    _configure(monkeypatch)

    def fake_get(url, headers=None):
        return _resp(404, {})

    with patch(_CLIENT, _client_returning(fake_get)):
        out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())

    assert out["reachable"] is False
    assert "no Voice PE entities" in out["reason"]


def test_unavailable_state_normalized_to_none(monkeypatch):
    _configure(monkeypatch)

    def fake_get(url, headers=None):
        if url.endswith("internal_temperature"):
            return _resp(200, _state("unavailable", "°F"))
        return _resp(200, _state("5"))

    with patch(_CLIENT, _client_returning(fake_get)):
        out = asyncio.run(pe_vitals.fetch_voice_pe_vitals())

    assert out["reachable"] is True
    assert out["temperature"] is None
    assert "temperature_unit" not in out  # no unit attached to a normalized-None field
