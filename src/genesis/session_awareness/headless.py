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
import logging
import os
from pathlib import Path

from genesis.util.proc_kill import kill_process_group, reap_bounded

logger = logging.getLogger(__name__)

# Dedicated working directory for ambient judges — deliberately NOT
# background_session_dir(), and not nested beneath it. Dispatched background
# sessions (research/interact/campaign profiles) retain the Write tool and
# run FROM that shared directory, so an injected one can drop a CLAUDE.md or
# a project .claude/settings.json there — which CC discovers from the
# child's cwd: instructions for every later judge, or hooks that EXECUTE.
# Tool denial does not stop instruction poisoning; a judge-owned cwd closes
# the CWD-LEVEL surfaces. Honest boundary: CC also walks ANCESTOR
# directories for memory files, and that level is a separate write-guard's
# job (tracked follow-up) — this dir closes what a shared cwd exposed, not
# every path a fully unscoped writer has. A SIBLING under ~/.genesis keeps
# the property the background dir was chosen for (outside any git repo, so
# CC's resume picker never lists these transcripts) without inheriting the
# shared directory itself as an ancestor. New file-plane justification
# (anti-proliferation gate): the existing store IS the trust boundary being
# escaped; the dir holds no data of its own — judges write nothing here,
# and the accessor ENFORCES that emptiness rather than assuming it.
_AMBIENT_JUDGE_DIR = Path.home() / ".genesis" / "ambient-judges"

# Context-bearing names CC would honor in the judge cwd. Anything matching
# is foreign by definition ("judges write nothing here") and is removed
# before every spawn — the emptiness invariant is enforced, not assumed.
_FOREIGN_CONTEXT = ("CLAUDE.md", "CLAUDE.local.md", ".mcp.json")


def _ambient_judge_dir() -> str:
    """Provision the isolated judge cwd and enforce its emptiness.

    Empty-state safe (fresh install: mkdir). Any context-bearing file found
    here was planted by something else — remove it and say so loudly; the
    log line is the tripwire for the write-guard follow-up.
    """
    _AMBIENT_JUDGE_DIR.mkdir(parents=True, exist_ok=True)
    for name in _FOREIGN_CONTEXT:
        f = _AMBIENT_JUDGE_DIR / name
        try:
            if f.exists():
                f.unlink()
                logger.error(
                    "ambient-judge dir contained foreign context file %s — "
                    "removed (nothing should ever write here)", name,
                )
        except OSError:
            logger.error("could not remove foreign context file %s", f, exc_info=True)
    proj = _AMBIENT_JUDGE_DIR / ".claude"
    try:
        if proj.is_dir():
            import shutil

            shutil.rmtree(proj, ignore_errors=True)
            logger.error(
                "ambient-judge dir contained a planted .claude/ project dir — "
                "removed (project hooks would EXECUTE in judge spawns)",
            )
    except OSError:
        logger.error("could not inspect %s", proj, exc_info=True)
    return str(_AMBIENT_JUDGE_DIR)


def build_argv(
    model: str,
    claude_path: str = "claude",
    no_mcp_config: str | None = None,
) -> list[str]:
    """The pinned headless argv (mirrors guardian/diagnosis.py).

    No ``--effort``: the ambient call sites pin Haiku, which doesn't take
    one. ``--strict-mcp-config`` + the repo's no_mcp.json keep MCP
    servers out of the subprocess, and ``--disallowedTools "*"`` denies
    every BUILT-IN tool as well: these are pure-completion judges over
    text that includes EXTERNAL content (PR titles/bodies via repo-pulse),
    and the child runs outside the project tree where no project guard
    loads. Measured (2026-09-04, execution-proof probe: a touch via the
    Bash tool): default-deny is NOT reliable headlessly — the tool ran
    without ``--dangerously-skip-permissions`` under this install's user
    settings — while the ``"*"`` deny stopped it; a name-enumerated deny
    list would silently reopen with every new built-in.
    """
    if no_mcp_config is None:
        # Deferred: only resolved when the caller didn't pin a config.
        from genesis.env import repo_root

        no_mcp_config = str(repo_root() / "config" / "no_mcp.json")
    # The child runs from _AMBIENT_JUDGE_DIR (not the parent's cwd), so any
    # RELATIVE path in the argv would resolve against the wrong directory (a
    # relative GENESIS_REPO_ROOT flowing through repo_root()). Anchor
    # path-shaped values to the PARENT's cwd now; a bare command word
    # (``claude``) stays bare so PATH lookup is untouched.
    no_mcp_config = os.path.abspath(no_mcp_config)
    if os.sep in claude_path:
        claude_path = os.path.abspath(claude_path)
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
        "--disallowedTools",
        "*",
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
            # Run OUTSIDE the project tree, in the judges' OWN directory
            # (see _AMBIENT_JUDGE_DIR): the transcript lands under a
            # non-interactive project key, so CC's resume picker never
            # lists ambient workers, the repo's SessionStart hooks don't
            # inject context into a one-turn judgment call, and no
            # tool-enabled session shares the cwd CC auto-discovers
            # CLAUDE.md from. Measured 2026-09-04: without this,
            # arbiter/ledger/repo-pulse transcripts accumulated in the
            # interactive project dir and surfaced in /resume.
            cwd=_ambient_judge_dir(),
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
