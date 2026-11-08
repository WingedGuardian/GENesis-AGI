"""Integration test — real CC CLI invocation.

Skipped unless opted in with GENESIS_LIVE_E2E=1 (it calls the real Claude API),
and inside a Claude Code session (CLAUDECODE env var set).
"""

import os
import shutil

import pytest

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation
from tests._live_fence import _S

_skip_no_claude = pytest.mark.skipif(
    os.environ.get("GENESIS_LIVE_E2E") != "1" or shutil.which("claude", path=_S.orig_path) is None,
    reason="requires an installed Claude CLI and opt-in with GENESIS_LIVE_E2E=1",
)
_skip_inside_cc = pytest.mark.skipif(
    os.environ.get("CLAUDECODE") == "1",
    reason="Cannot nest CC sessions",
)


@_skip_no_claude
@_skip_inside_cc
@pytest.mark.real_tool("claude")
async def test_real_cc_invocation():
    """Invoke claude -p with a trivial prompt and verify output parsing."""
    invoker = CCInvoker()
    output = await invoker.run(
        CCInvocation(
            prompt="Respond with exactly: hello",
            output_format="json",
            timeout_s=60,
        ),
    )
    assert not output.is_error, f"CC invocation failed: {output.error_message}"
    assert output.exit_code == 0
    assert len(output.text) > 0


@_skip_no_claude
@_skip_inside_cc
@pytest.mark.real_tool("claude")
async def test_real_cc_with_system_prompt():
    """Invoke claude -p with a system prompt."""
    invoker = CCInvoker()
    output = await invoker.run(
        CCInvocation(
            prompt="What are you?",
            system_prompt="You are Genesis. Respond in one sentence.",
            output_format="json",
            timeout_s=60,
        ),
    )
    assert not output.is_error, f"CC invocation failed: {output.error_message}"
    assert len(output.text) > 0
