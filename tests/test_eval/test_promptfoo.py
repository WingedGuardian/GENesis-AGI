"""Tests for genesis.eval.promptfoo subprocess handling.

promptfoo (``npx promptfoo`` → node → workers) is a launcher: on timeout the
old ``subprocess.run`` kill reached only the direct child and orphaned the
worker tree — the class fixed repo-wide by the spawn-hardening sweep.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from genesis.eval.promptfoo import compare_models


def _popen_mock(pid: int = 61234) -> MagicMock:
    m = MagicMock()
    m.pid = pid  # explicit — never a mock default (the killpg(1) trap)
    m.returncode = 0
    m.communicate.return_value = ("", "")
    return m


@pytest.mark.asyncio
async def test_timeout_returns_failed_report_and_group_kills(tmp_path: Path):
    proc = _popen_mock()
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="promptfoo", timeout=1),
        ("", ""),  # bounded drain
    ]
    with (
        patch("genesis.eval.promptfoo.subprocess.Popen", return_value=proc),
        patch("genesis.util.proc_kill.os.killpg") as killpg,
    ):
        report = await compare_models(
            model_a="a",
            model_b="b",
            dataset_path=tmp_path / "ds.yaml",
            timeout_s=1,
        )
    assert report.success is False
    assert "timed out" in (report.error or "")
    killpg.assert_called_once()
    assert killpg.call_args.args[0] == 61234
    proc.kill.assert_not_called()
    # the drain was attempted bounded, not blocking
    assert proc.communicate.call_args_list[1].kwargs.get("timeout") is not None


@pytest.mark.asyncio
async def test_spawned_in_new_session_not_preexec(tmp_path: Path):
    captured: dict = {}

    def _fake_popen(*args, **kwargs):
        captured.update(kwargs)
        m = _popen_mock()
        # rc != 0 short-circuits before output-file parsing
        m.returncode = 1
        m.communicate.return_value = ("", "boom")
        return m

    with patch("genesis.eval.promptfoo.subprocess.Popen", side_effect=_fake_popen):
        report = await compare_models(
            model_a="a",
            model_b="b",
            dataset_path=tmp_path / "ds.yaml",
            timeout_s=1,
        )
    assert report.success is False  # rc=1 path — spawn shape is what we assert
    assert captured.get("start_new_session") is True
    assert "preexec_fn" not in captured


@pytest.mark.asyncio
async def test_keyboard_interrupt_kills_group_and_propagates(tmp_path: Path):
    """subprocess.run killed the child on ANY exception (incl. Ctrl+C); the
    Popen migration must preserve that — and with start_new_session the
    terminal SIGINT no longer reaches the tree ambiently, so the handler is
    the only thing standing between Ctrl+C and a leaked promptfoo tree."""
    proc = _popen_mock()
    proc.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
    with (
        patch("genesis.eval.promptfoo.subprocess.Popen", return_value=proc),
        patch("genesis.util.proc_kill.os.killpg") as killpg,
        pytest.raises(KeyboardInterrupt),
    ):
        await compare_models(
            model_a="a", model_b="b",
            dataset_path=tmp_path / "ds.yaml", timeout_s=600,
        )
    killpg.assert_called_once()
    assert killpg.call_args.args[0] == 61234
