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
  spawns MCP children; killing only the parent orphans them) via the
  shared guarded helper (``genesis.util.proc_kill``).
- Never raises: every outcome is a status dict.
"""

from __future__ import annotations

import asyncio
import os

from genesis.cc.types import background_session_dir
from genesis.util.proc_kill import kill_process_group, reap_bounded


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
            # Run OUTSIDE the project tree (same convention as CCInvoker
            # dispatches): the child's transcript lands under the
            # background-sessions project key, so CC's resume picker never
            # lists ambient workers next to interactive sessions, and the
            # repo's SessionStart hooks don't inject context into a
            # one-turn judgment call. Measured 2026-09-04: without this,
            # arbiter/ledger/repo-pulse transcripts accumulated in the
            # interactive project dir and surfaced in /resume.
            cwd=background_session_dir(),
            # Own session/group (setsid in the C helper — never preexec_fn:
            # post-fork Python can deadlock in the threaded server) so the
            # timeout below can killpg the whole claude tree.
            start_new_session=True,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()),
                timeout=timeout_s,
            )
        except TimeoutError:
            # claude spawns MCP/helper children — group-kill is mandatory.
            # kill_process_group signals proc.pid AS the pgid (never
            # os.getpgid, which raises once the leader is reaped and would
            # leak the children), with the pgid>1 guard + direct-kill
            # fallback; the reap is bounded (a paused pipe transport can
            # stall an unbounded wait()).
            kill_process_group(proc)
            await reap_bounded(proc)
            return {"status": "timeout"}
        except asyncio.CancelledError:
            # Task cancellation: the detached child sees no ambient signal —
            # group-kill before propagating or the tree leaks.
            kill_process_group(proc)
            await reap_bounded(proc)
            raise
        if proc.returncode != 0:
            return {"status": "failed", "reason": f"exit_{proc.returncode}"}
        return {"status": "ok", "stdout": stdout.decode(errors="replace")}
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
