"""Pure shared config and metadata admission, independent of installed SDKs."""

import pytest

from genesis.mcp.shared_http._settings import SharedHTTPConfig, require_qualified_stack


def settings(**overrides):
    return SharedHTTPConfig(**{
        "service": "health", "url": "http://127.0.0.1:8100/mcp", "db_units": 2,
        "tool_concurrency": 2, "max_sessions": 3, "session_idle_timeout_s": 30,
        "max_body_bytes": 4096, **overrides,
    })


@pytest.mark.parametrize("url,authority", [
    ("http://127.0.0.1:8100/mcp", "127.0.0.1:8100"),
    ("http://127.1.2.3:8100/mcp", "127.1.2.3:8100"),
    ("http://[::1]:8100/mcp", "[::1]:8100"),
    ("http://[0:0:0:0:0:0:0:1]:8100/mcp", "[0:0:0:0:0:0:0:1]:8100"),
])
def test_exact_configured_loopback_authority(url, authority):
    config = settings(url=url)
    assert config.authority == authority
    assert config.origin == "http://" + authority


@pytest.mark.parametrize("url", [
    "http://localhost:8100/mcp", "http://192.0.2.1:8100/mcp", "http://0.0.0.0:8100/mcp",
    "https://127.0.0.1:8100/mcp", "http://127.0.0.1/mcp", "http://127.0.0.1:0/mcp",
    "http://127.0.0.1:65536/mcp", "http://user@127.0.0.1:8100/mcp",
    "http://127.0.0.1:8100/mcp?x=1", "http://127.0.0.1:8100/mcp#x",
    " http://127.0.0.1:8100/mcp", "http://127.0.0.1:8100/mcp\n",
    "http://[::1%lo]:8100/mcp", "http://127.0.0.1:8100/mcp/", "http://127.0.0.1:8100//mcp",
    "http://127.0.0.1:*/mcp", "http://0177.0.0.1:8100/mcp", "http://127.0.0.1:/mcp",
])
def test_invalid_bind_configuration_refuses(url):
    with pytest.raises(ValueError):
        settings(url=url)


@pytest.mark.parametrize("field", ["db_units", "tool_concurrency", "max_sessions", "max_body_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.2, "2", None])
def test_count_limits_are_explicit_positive_integers(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "2", None])
def test_idle_limit_is_finite_positive_and_not_bool(value):
    with pytest.raises(ValueError):
        settings(session_idle_timeout_s=value)


def test_concurrency_cannot_exceed_foreground_budget():
    with pytest.raises(ValueError):
        settings(tool_concurrency=3)
    with pytest.raises(ValueError):
        settings(service="discord-bot")


def test_version_fence_is_exact_and_missing_is_not_qualified(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from genesis.mcp.shared_http import _settings

    versions = {"fastmcp": "4.1.4", "mcp": "2.3.0", "uvicorn": "0.54.0"}
    monkeypatch.setattr(_settings, "version", versions.__getitem__)
    require_qualified_stack()
    versions["fastmcp"] = "4.1.5"
    with pytest.raises(RuntimeError):
        require_qualified_stack()

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(_settings, "version", missing)
    with pytest.raises(RuntimeError):
        require_qualified_stack()
