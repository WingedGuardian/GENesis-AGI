"""Shared `gh` CLI runner for recon jobs.

Single source of truth for invoking the GitHub CLI via subprocess with a
timeout and uniform failure handling (the process-kill sequence on timeout is
easy to get subtly wrong, so it lives here rather than being copy-pasted).
Both ``ReconGatherer`` and ``github_discovery`` use this.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

# Network calls are slower than local git — generous default ceiling.
_DEFAULT_TIMEOUT = 15


async def run_gh(*args: str, timeout: int | float = _DEFAULT_TIMEOUT) -> str:
    """Run a ``gh`` command with a timeout. Returns stdout, or "" on any failure.

    Failure (non-zero exit, timeout, or spawn error) is logged and collapsed to
    an empty string so callers can treat "no data" uniformly. On timeout the
    child process is killed and reaped to avoid leaks.
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            logger.warning(
                "gh command failed (rc=%s): %s — %s",
                proc.returncode,
                " ".join(args),
                stderr.decode("utf-8", errors="replace")[:200],
            )
            return ""
        return stdout.decode("utf-8", errors="replace").strip()
    except TimeoutError:
        logger.warning("gh command timed out: %s", " ".join(args))
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
        return ""
    except OSError:
        logger.warning("gh command failed to start: %s", " ".join(args), exc_info=True)
        return ""
