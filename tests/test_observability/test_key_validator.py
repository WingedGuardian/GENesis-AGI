"""Unit tests for the extracted API-key validator helpers.

``build_key_validator`` (pure URL/header builder) and ``test_single_key`` (live
one-shot HTTP validation) are shared by the scheduled ``validate_api_keys`` and
the dashboard first-run key test. No network — httpx is faked.
"""

from __future__ import annotations

from genesis.observability.snapshots import api_keys


def test_build_key_validator_known_providers():
    url, headers = api_keys.build_key_validator("anthropic", "sk-test")
    assert url == "https://api.anthropic.com/v1/models"
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"

    url, headers = api_keys.build_key_validator("openrouter", "or-key")
    assert "openrouter.ai" in url
    assert headers["Authorization"] == "Bearer or-key"

    # google carries the key in the query string, no auth header
    url, headers = api_keys.build_key_validator("google", "g-key")
    assert "key=g-key" in url
    assert headers == {}


def test_build_key_validator_zenmux_default_and_custom_base_url():
    url, _ = api_keys.build_key_validator("zenmux", "z")
    assert url == "https://zenmux.ai/api/v1/models"
    url2, _ = api_keys.build_key_validator("zenmux", "z", base_url="https://custom.example/api")
    assert url2 == "https://custom.example/api/models"


def test_build_key_validator_unknown_returns_none():
    assert api_keys.build_key_validator("ollama", "x") is None
    assert api_keys.build_key_validator("", "x") is None


class _FakeResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        if self._raise is not None:
            raise self._raise
        return self._resp


async def test_test_single_key_valid(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(_FakeResp(200)))
    assert await api_keys.test_single_key("anthropic", "sk-good") == {"valid": True}


async def test_test_single_key_invalid_http(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _FakeClient(_FakeResp(401, "bad key"))
    )
    result = await api_keys.test_single_key("anthropic", "sk-bad")
    assert result["valid"] is False
    assert "401" in result["error"]


async def test_test_single_key_network_error(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _FakeClient(raise_exc=httpx.ConnectError("down"))
    )
    result = await api_keys.test_single_key("anthropic", "sk")
    assert result["valid"] is False
    assert "down" in result["error"]


async def test_test_single_key_unknown_provider():
    result = await api_keys.test_single_key("ollama", "x")
    assert result["valid"] is False
    assert "ollama" in result["error"]
