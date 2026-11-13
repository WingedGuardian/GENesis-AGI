"""Read complete local JSON lines without silently clipping owner content."""

import asyncio


async def read_line(reader: asyncio.StreamReader) -> bytes:
    parts = []
    while True:
        try:
            parts.append(await reader.readuntil(b"\n"))
            break
        except asyncio.LimitOverrunError as exc:
            parts.append(await reader.readexactly(exc.consumed))
        except asyncio.IncompleteReadError as exc:
            parts.append(exc.partial)
            break
    return b"".join(parts)
