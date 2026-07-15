"""Shared headless-CC subprocess runner for ambient session tooling.

Extracted from ``arbiter.judge_candidates`` (WS-C #977) so the ledger
shadow extractor (session-manager PR-3) doesn't grow a third copy of the
spawn/env/group-kill machinery (``guardian/diagnosis.py`` predates the
extraction and keeps its own). Locked invariants carried over verbatim:

- ``GENESIS_CC_SESSION=1`` in the child env — a nested claude subprocess
  must never re-enter Genesis hooks.
- ``GENESIS_SESSION_ORIGIN`` popped — WS-3: never leak a session origin
  into the nested subprocess (mirrors ``CCInvoker._build_env``).
- ONE timeout; on expiry the whole PROCESS GROUP is SIGKILLed (claude
  spawns MCP children; killing only the parent orphans them) — with the
  pgid>1 guard from ``cc/invoker.py``.
- Never raises: every outcome is a status dict.
"""

from __future__ import annotations

import asyncio
import os
import signal


def build_argv(
    model: str,
    claude_path: str = "claude",
    no_mcp_config: str | None = None,
) -> list[str]:
    """The pinned headless argv (mirrors guardian/diagnosis.py).

    No ``--effort``: the ambient call sites pin Haiku, which doesn't take
    one. ``--strict-mcp-config`` + the repo's no_mcp.json keep MCP
    servers out of the subprocess.
    """
    if no_mcp_config is None:
        # Deferred: only resolved when the caller didn't pin a config.
        from genesis.env import repo_root

        no_mcp_config = str(repo_root() / "config" / "no_mcp.json")
    return [
        claude_path,
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--dangerously-skip-permissions",
        "--mcp-config",
        no_mcp_config,
        "--strict-mcp-config",
    ]


async def run_headless_json(
    prompt: str,
    *,
    model: str,
    claude_path: str = "claude",
    no_mcp_config: str | None = None,
    timeout_s: float,
) -> dict:
    """One headless claude call. Returns a status dict, never raises.

    ``{"status": "ok", "stdout": <str>}`` on a zero exit;
    ``{"status": "timeout"}`` after a group-kill;
    ``{"status": "failed", "reason": <str>}`` on a nonzero exit
    (``exit_<code>``) or any spawn/communicate exception.
    Output parsing is the caller's job — parsers are call-site-specific
    and fail-closed there.
    """
    try:
        argv = build_argv(model, claude_path, no_mcp_config)
        env = dict(os.environ)
        env["GENESIS_CC_SESSION"] = "1"  # never re-enter Genesis hooks
        # WS-3: never leak a session origin into the nested claude
        # subprocess (mirrors CCInvoker._build_env's pop invariant).
        env.pop("GENESIS_SESSION_ORIGIN", None)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            preexec_fn=os.setpgrp,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()),
                timeout=timeout_s,
            )
        except TimeoutError:
            # claude spawns MCP/helper children — group-kill is mandatory
            # (cc/invoker.py pattern, incl. the pgid>1 safety guard).
            try:
                pgid = os.getpgid(proc.pid)
                if pgid <= 1:
                    raise ValueError(f"Refusing killpg with pgid={pgid}")
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, ValueError, TypeError):
                proc.kill()
            await proc.wait()
            return {"status": "timeout"}
        if proc.returncode != 0:
            return {"status": "failed", "reason": f"exit_{proc.returncode}"}
        return {"status": "ok", "stdout": stdout.decode(errors="replace")}
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
