"""Shared-only configuration, importable before SDK or resource construction."""

import math
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from ipaddress import ip_address
from urllib.parse import urlsplit


def require_qualified_stack() -> None:
    for package, expected in (("fastmcp", "4.1.4"), ("mcp", "2.3.0"), ("uvicorn", "0.54.0")):
        try:
            actual = version(package)
        except PackageNotFoundError:
            raise RuntimeError("Shared MCP requires the qualified stack") from None
        if actual != expected:
            raise RuntimeError(f"Unqualified shared MCP dependency: {package}")


@dataclass(frozen=True)
class SharedHTTPConfig:
    service: str
    url: str
    db_units: int
    tool_concurrency: int
    max_sessions: int
    session_idle_timeout_s: float
    max_body_bytes: int

    def __post_init__(self):
        if not isinstance(self.service, str) or self.service not in {"health", "memory", "outreach", "recon"}:
            raise ValueError("Invalid shared service")
        for name in ("db_units", "tool_concurrency", "max_sessions", "max_body_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"Invalid shared limit: {name}")
        idle = self.session_idle_timeout_s
        try:
            valid_idle = (not isinstance(idle, bool) and isinstance(idle, (int, float))
                          and math.isfinite(idle) and idle > 0)
        except OverflowError:
            valid_idle = False
        if not valid_idle:
            raise ValueError("Invalid shared limit: session_idle_timeout_s")
        if self.tool_concurrency > self.db_units:
            raise ValueError("Tool concurrency exceeds foreground database budget")
        try:
            if not isinstance(self.url, str) or any(ord(c) < 33 or ord(c) > 126 for c in self.url):
                raise ValueError
            parsed = urlsplit(self.url)
            if (parsed.scheme != "http" or parsed.path != "/mcp" or "?" in self.url or "#" in self.url
                    or "%" in parsed.netloc or parsed.username is not None or parsed.password is not None
                    or not parsed.port or not ip_address(parsed.hostname).is_loopback):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid shared loopback URL") from None

    @property
    def authority(self) -> str:
        return urlsplit(self.url).netloc

    @property
    def origin(self) -> str:
        return "http://" + self.authority
