"""The `deliberate` MCP tool records each run to the model_fusion call site.

The Fusion backend POSTs OpenRouter over raw httpx — outside the router and
call_site_recorder — so the MCP wrapper (`_impl_deliberate`) is the one seam
that observes every invocation. These lock that it records to `model_fusion`
(success + failure) and that recording is strictly best-effort (a recorder
failure must never break the tool).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.deliberation import DeliberationResult
from genesis.mcp.health.deliberation_tools import _impl_deliberate


def _result(**kw) -> DeliberationResult:
    base: dict = {"answer": "verdict", "preset_used": "budget", "error": None}
    base.update(kw)
    return DeliberationResult(**base)


@pytest.mark.asyncio
async def test_records_model_fusion_on_success():
    rec = AsyncMock(return_value=True)
    with (
        patch("genesis.deliberation.deliberate", AsyncMock(return_value=_result())),
        patch("genesis.observability.call_site_recorder.record_last_run_detached", rec),
    ):
        out = await _impl_deliberate("Should we ship?", preset="budget")
    assert out["answer"] == "verdict"
    rec.assert_awaited_once()
    args, kwargs = rec.call_args
    assert args[1] == "model_fusion"  # call_site_id
    assert kwargs["provider"] == "openrouter"
    assert kwargs["model_id"] == "fusion:budget"
    assert kwargs["success"] is True


@pytest.mark.asyncio
async def test_records_failure_when_error_set():
    rec = AsyncMock(return_value=True)
    with (
        patch(
            "genesis.deliberation.deliberate",
            AsyncMock(return_value=_result(answer=None, error="panel timeout")),
        ),
        patch("genesis.observability.call_site_recorder.record_last_run_detached", rec),
    ):
        out = await _impl_deliberate("Q?", preset="budget")
    assert out["error"] == "panel timeout"
    rec.assert_awaited_once()
    _, kwargs = rec.call_args
    assert kwargs["success"] is False


@pytest.mark.asyncio
async def test_recording_is_best_effort():
    """A recorder exception must be swallowed — the tool still returns its verdict."""
    with (
        patch("genesis.deliberation.deliberate", AsyncMock(return_value=_result())),
        patch(
            "genesis.observability.call_site_recorder.record_last_run_detached",
            AsyncMock(side_effect=RuntimeError("db gone")),
        ),
    ):
        out = await _impl_deliberate("Q?", preset="budget")
    assert out["answer"] == "verdict"
    assert out["error"] is None


@pytest.mark.asyncio
async def test_empty_question_short_circuits_without_recording():
    """The early input-validation return must not record a run."""
    rec = AsyncMock(return_value=True)
    with patch("genesis.observability.call_site_recorder.record_last_run_detached", rec):
        out = await _impl_deliberate("   ")
    assert "error" in out
    rec.assert_not_awaited()
