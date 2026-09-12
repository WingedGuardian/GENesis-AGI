"""Shared `gh` CLI runner for recon jobs.

Single source of truth for invoking the GitHub CLI via subprocess with a
timeout and uniform failure handling (the process-kill sequence on timeout is
easy to get subtly wrong, so it lives here rather than being copy-pasted).
Both ``ReconGatherer`` and ``github_discovery`` use ``run_gh``; the account
activity monitor uses ``run_gh_checked`` where it must tell a real error apart
from an empty result.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

# Network calls are slower than local git — generous default ceiling.
_DEFAULT_TIMEOUT = 15


async def _exec_gh(args: tuple[str, ...], timeout: int | float) -> tuple[bool, str]:
    """Run a ``gh`` command. Returns ``(ok, stdout)``.

    ``ok`` is False on non-zero exit, timeout, or spawn error (with ``stdout``
    then ``""``). The timeout process-kill/reap sequence lives here so both
    public wrappers share exactly one implementation.
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
            return False, ""
        return True, stdout.decode("utf-8", errors="replace").strip()
    except TimeoutError:
        logger.warning("gh command timed out: %s", " ".join(args))
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
        return False, ""
    except OSError:
        logger.warning("gh command failed to start: %s", " ".join(args), exc_info=True)
        return False, ""


async def run_gh(*args: str, timeout: int | float = _DEFAULT_TIMEOUT) -> str:
    """Run a ``gh`` command. Returns stdout, or "" on any failure.

    Failure (non-zero exit, timeout, or spawn error) is logged and collapsed to
    an empty string so callers can treat "no data" uniformly. Use
    ``run_gh_checked`` when you must distinguish an error from a genuinely empty
    result (e.g. a cursor you must NOT advance on a failed poll).
    """
    _ok, stdout = await _exec_gh(args, timeout)
    return stdout


async def run_gh_checked(*args: str, timeout: int | float = _DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Like ``run_gh`` but returns ``(ok, stdout)``.

    ``ok`` is False iff the command failed (non-zero exit / timeout / spawn
    error) — letting the caller tell a real error apart from an empty-but-
    successful result, which ``run_gh`` collapses into the same ``""``.
    """
    return await _exec_gh(args, timeout)
