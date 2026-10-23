"""Fixed diagnostic commands under the contained receipt's original deadline."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import replace
from uuid import uuid4

from genesis.cc.checkout_lock import admit_launch
from genesis.cc.peer_segment import PeerSegment
from genesis.util.proc_kill import ProcessCleanupUncertain, spawn_owned


async def run_helper(invoker, invocation, command):
    # These two closed commands are server-owned, never supplied by a caller.
    if command not in {("auth", "status", "--json"), ("--version",)}:
        raise ValueError("Unsupported contained helper")
    policy = PeerSegment(uuid4().hex, invocation.peer_segment.deadline_at, (), mode="conversation")
    helper = replace(invocation, peer_segment=policy, on_spawn=None, session_key=None,
                     mcp_config=None, disable_tools=True)
    remaining = min(15.0, policy.deadline_at - time.time())
    if remaining <= 0:
        raise TimeoutError("Contained helper deadline expired")
    async with policy.invocation():
        async with asyncio.timeout(remaining):
            admission = await admit_launch()
            try:
                from genesis.cc.invoker import _SCOPE_PROPERTIES

                env = invoker._launch_env(invoker._build_env(helper), helper)
                args = policy.launch_args(
                    [invoker._claude_path, *policy.cli_flags(), *command], env,
                    helper.working_dir, _SCOPE_PROPERTIES,
                )
                try:
                    proc = await spawn_owned(
                        *args, env=env, cwd=helper.working_dir, start_new_session=True,
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except ProcessCleanupUncertain as exc:
                    if exc.process is not None:
                        invoker._register_proc(f"pid:{exc.process.pid}", exc.process, confirm_cleanup=True)
                    raise
                key = invoker._register_proc(f"pid:{proc.pid}", proc, confirm_cleanup=True)
            finally:
                admission.release()
            try:
                stdout, _ = await proc.communicate()
                return proc.returncode, stdout.decode("utf-8", errors="replace")
            finally:
                await invoker._confirm_registered_exit(proc, key)


async def logged_out(invoker, invocation):
    try:
        _, text = await run_helper(invoker, invocation, ("auth", "status", "--json"))
        value = json.loads(text)
        return isinstance(value, dict) and value.get("loggedIn") is False
    except ProcessCleanupUncertain:
        raise
    except (OSError, ValueError, TimeoutError):
        return False


async def version(invoker, invocation):
    """Reuse immutable successful versions; never write owner diagnostic caches."""
    from genesis.cc.exceptions import CCProcessError
    from genesis.cc.invoker import parse_cc_version

    try:
        resolved = shutil.which(invoker._claude_path)
        if resolved is None:
            return None
        real = os.path.realpath(resolved)
        stat = os.stat(real)
        cached = invoker._cc_versions.get((real, stat.st_ino, stat.st_mtime_ns))
        if cached is not None:
            return cached
        code, text = await run_helper(invoker, invocation, ("--version",))
        return parse_cc_version(text) if code == 0 else None
    except ProcessCleanupUncertain:
        raise
    except (OSError, ValueError, TimeoutError, CCProcessError):
        return None
