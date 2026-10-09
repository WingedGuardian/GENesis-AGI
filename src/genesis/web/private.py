"""Task-local suppression of third-party web diagnostics carrying private input."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

BODY_LIMIT = 1024 * 1024

_PRIVATE = ContextVar("genesis_private_web", default=False)


class _PrivateWebFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE.get()


_FILTER = _PrivateWebFilter()
# Originating loggers, not ancestors: ancestor filters do not filter propagation.
_LOGGERS = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


@contextmanager
def private_web() -> Iterator[None]:
    for name in _LOGGERS:
        logging.getLogger(name).addFilter(_FILTER)  # logging.addFilter is idempotent.
    token = _PRIVATE.set(True)
    try:
        yield
    finally:
        _PRIVATE.reset(token)


async def bounded_body(response: httpx.Response) -> bytes:
    """Identity-only streaming; refuse oversize before retaining more bytes."""
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise ValueError("Private web response refused")
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4096):
        if len(body) + len(chunk) > BODY_LIMIT:
            raise ValueError("Private web response refused")
        body.extend(chunk)
    return bytes(body)
