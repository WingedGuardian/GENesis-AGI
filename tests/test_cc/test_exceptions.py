"""Tests for CC exception hierarchy."""

from genesis.cc.exceptions import (
    CCError,
    CCMCPError,
    CCParsingError,
    CCProcessError,
    CCRateLimitError,
    CCSessionError,
    CCTimeoutError,
)


def test_hierarchy_all_inherit_from_ccerror():
    """All CC exceptions inherit from CCError."""
    for exc_cls in (
        CCTimeoutError,
        CCProcessError,
        CCParsingError,
        CCSessionError,
        CCMCPError,
        CCRateLimitError,
    ):
        assert issubclass(exc_cls, CCError)
        assert issubclass(exc_cls, Exception)


def test_ccmcp_error_server_name():
    """CCMCPError stores optional server_name."""
    e = CCMCPError("MCP failed", server_name="memory")
    assert e.server_name == "memory"
    assert str(e) == "MCP failed"


def test_ccmcp_error_no_server_name():
    """CCMCPError works without server_name."""
    e = CCMCPError("MCP failed")
    assert e.server_name is None


def test_exceptions_catchable_as_base():
    """Catching CCError catches all subtypes."""
    for exc_cls in (CCTimeoutError, CCSessionError, CCRateLimitError):
        try:
            raise exc_cls("test")
        except CCError:
            pass  # Expected
        else:
            raise AssertionError(f"{exc_cls.__name__} not caught by CCError")
