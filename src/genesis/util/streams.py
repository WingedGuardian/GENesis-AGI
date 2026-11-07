"""Retain bounded stream prefixes while counting and draining full output.

The caller owns deadlines and process cleanup. Non-positive limits retain no
bytes; this reader does not reject them or impose a time limit.
"""

from __future__ import annotations

import asyncio

DEFAULT_STREAM_LIMIT = 2 * 1024 * 1024


async def read_limited(
    stream: asyncio.StreamReader,
    limit: int = DEFAULT_STREAM_LIMIT,
) -> tuple[bytes, int]:
    """Retain up to *limit* bytes while counting and draining the full stream."""
    chunks: list[bytes] = []
    retained_size = 0
    total_size = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        total_size += len(chunk)
        remaining = limit - retained_size
        if remaining <= 0:
            continue
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            retained_size = limit
        else:
            chunks.append(chunk)
            retained_size += len(chunk)
    return b"".join(chunks), total_size
