"""Tests for the shared gh CLI runner (genesis.recon.gh_cli)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from genesis.recon.gh_cli import run_gh


def _mock_subprocess(stdout: str, returncode: int = 0):
    """Build a fake asyncio.create_subprocess_exec returning given stdout/rc."""

    async def create_subprocess(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
        return proc

    return create_subprocess


@pytest.mark.asyncio
async def test_success_returns_stripped_stdout():
    with patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess('  ["ok"]  ')):
        result = await run_gh("gh", "api", "test")
    assert result == '["ok"]'


@pytest.mark.asyncio
async def test_nonzero_exit_returns_empty():
    with patch("asyncio.create_subprocess_exec", side_effect=_mock_subprocess("boom", returncode=1)):
        result = await run_gh("gh", "api", "test")
    assert result == ""


@pytest.mark.asyncio
async def test_timeout_returns_empty_and_kills_process():
    captured: dict = {}

    async def slow_subprocess(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0

        async def slow_communicate():
            await asyncio.sleep(100)
            return b"", b""

        proc.communicate = slow_communicate
        captured["proc"] = proc
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=slow_subprocess):
        result = await run_gh("gh", "api", "test", timeout=0.05)

    assert result == ""
    captured["proc"].kill.assert_called_once()
    captured["proc"].wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_os_error_returns_empty():
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("gh not found")):
        result = await run_gh("gh", "api", "test")
    assert result == ""
