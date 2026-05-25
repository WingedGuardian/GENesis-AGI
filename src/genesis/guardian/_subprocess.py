"""Shared async subprocess runner for Guardian host-side operations."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_subprocess(
    *args: str, timeout: float = 10.0,
) -> tuple[int, str, str]:
    """Run a subprocess with timeout. Returns (returncode, stdout, stderr)."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )
    except TimeoutError:
        # Kill if still running
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return -1, "", "timeout"
    except OSError as exc:
        logger.warning("Subprocess exec failed for %s: %s", args[0] if args else "?", exc)
        return -1, "", str(exc)
    except Exception as exc:
        logger.error("Unexpected subprocess error: %s", exc, exc_info=True)
        return -1, "", str(exc)
