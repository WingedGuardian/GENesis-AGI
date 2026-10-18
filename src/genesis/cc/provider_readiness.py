"""Tool-disabled provider readiness, separate from executing the caller's work."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from genesis.cc.exceptions import CCProcessError, CCRateLimitError
from genesis.cc.types import PROBE_CALLER_TAG, CCInvocation, EffortLevel
from genesis.util.execution_privacy import private_execution
from genesis.util.proc_kill import ProcessCleanupUncertain
from genesis.util.tmp import big_tmp_dir

READY = "GENESIS_PROVIDER_READY"
_phase = ContextVar("provider_readiness_phase", default=None)


@contextmanager
def isolated_provider(*, probe=False):
    """Keep routed calls from changing shared home-provider status callbacks."""
    token = _phase.set("probe" if probe else "execution")
    try:
        yield
    finally:
        _phase.reset(token)


def probing_provider() -> bool:
    return _phase.get() == "probe"


def provider_status_isolated() -> bool:
    return _phase.get() is not None


async def probe_provider(invoker, base: CCInvocation, *, timeout_s: float):
    """Require enforced empty authority and a complete, exact readiness answer.

    The supplied invocation contributes only provider routing and model tier;
    no task content, MCP config, hooks, tool grants or resume travels here.
    Uncertain cleanup keeps the directory for the possibly live process.
    """
    root = Path(tempfile.mkdtemp(prefix="provider-ready-", dir=big_tmp_dir()))
    empty_mcp = root / "mcp.json"
    empty_mcp.write_text('{"mcpServers":{}}', encoding="utf-8")
    probe = CCInvocation(
        prompt=f"Provider availability check. Reply exactly {READY}.",
        model=base.model,
        effort=EffortLevel.LOW,
        safe_mode=True,
        disable_tools=True,
        no_session_persistence=True,
        mcp_config=str(empty_mcp),
        strict_mcp_config=True,
        working_dir=str(root),
        claude_code_tmpdir=str(root / "tmp"),
        timeout_s=timeout_s,
        confirm_cleanup=True,
        origin=base.origin,
        supervised=False,
        roster_eligible=bool(base.model_id_override),
        anthropic_base_url=base.anthropic_base_url,
        anthropic_auth_token=base.anthropic_auth_token,
        model_id_override=base.model_id_override,
        caller_tag=PROBE_CALLER_TAG,
    )
    empty_init = False
    last_limit = None
    uncertain = False

    async def observe(event):
        nonlocal empty_init, last_limit
        raw = event.raw or {}
        if raw.get("type") == "rate_limit_event":
            last_limit = raw
        if raw.get("type") == "system" and raw.get("subtype") == "init":
            empty_init = raw.get("tools") == [] and raw.get("mcp_servers") == []
            if not empty_init:
                raise CCProcessError("Provider readiness authority is unavailable")

    try:
        with isolated_provider(probe=True), private_execution():
            output = await invoker.run_streaming(probe, on_event=observe)
        if (
            not empty_init
            or output.is_error
            or not output.result_received
            or output.bg_truncated
            or output.stream_lines_dropped
            or output.tools_used != ()
            or output.text.strip() != READY
        ):
            raise CCProcessError("Provider readiness was not established")
        # CC also emits informational limits and usable-overage states. Use
        # the last effective state, as the CLI does; a later allowed update can
        # supersede a rejection encountered while the diagnostic was retrying.
        info = (last_limit or {}).get("rate_limit_info") or {}
        if info.get("status") == "rejected" and info.get("isUsingOverage") is not True:
            raise CCRateLimitError("Provider readiness was refused", raw_event=last_limit)
        return output
    except ProcessCleanupUncertain as exc:
        uncertain = True
        exc.probe_directory = root
        raise
    finally:
        if not uncertain:
            shutil.rmtree(root, ignore_errors=True)
