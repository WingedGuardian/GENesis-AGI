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
import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from genesis.util.proc_kill import kill_process_group, reap_bounded

logger = logging.getLogger(__name__)

# Per-call working directory for ambient judges, under one stable parent.
#
# The judge child must not inherit CONTEXT it did not author: CC reads
# CLAUDE.md / CLAUDE.local.md / .mcp.json from its cwd, and a project
# .claude/settings.json there gives it HOOKS THAT EXECUTE. Tool denial does
# not stop instruction poisoning, and dispatched background sessions
# (research/interact/campaign) hold Write with no path scope — so any
# directory that outlives a call is a plantable surface.
#
# Rather than defend a fixed directory, each call gets a FRESH one: nothing
# can PRE-plant in a directory whose name did not exist a moment ago. Three
# review rounds went into finding holes in a defended-fixed-directory design
# (a shared parent, then a sweep a symlink walked past); this shape has no
# interior at the cwd level for such a hole to live in — no sweep, no
# tripwire, no symlink case there.
#
# HONEST BOUNDARY — what this does NOT close, MEASURED 2026-09-05 with the
# shipped argv against a real child: CC also reads memory files from every
# ANCESTOR of the cwd, and always loads the user-level ~/.claude/CLAUDE.md.
# A memory file placed in this parent, in ~/.genesis, or in the home
# directory is therefore read by every judge — the cwd being fresh does not
# make the directories ABOVE it fresh. Constraining what may write those
# paths is a separate control and is tracked as such; do not read this
# design as closing it. (Parent-level .claude/settings.json hooks do NOT
# execute: CC resolves the project root from the cwd, which has none.)
#
# Residual within scope, accepted: a RESIDENT same-uid process can watch
# this parent and write into a per-call directory between its creation and
# the child reading it — 0700 is no barrier to the same uid. The redesign
# converts a plant-once-poison-every-future-call attack into one needing
# continuous presence and a won race. A real reduction, not an elimination.
#
# The stable PARENT gives the debris one predictable home for disk hygiene
# to target (transcript-retention issue #1709); it is never itself a cwd.
#
# Out of any git repo, as before, so CC's resume picker never lists these
# one-turn judgments beside interactive sessions.
_AMBIENT_JUDGE_ROOT = Path.home() / ".genesis" / "ambient-judges"


@contextlib.contextmanager
def _judge_cwd() -> Iterator[str]:
    """A fresh, private working directory for one judge call.

    Yields the path; removes it on exit, including after a timeout or a
    cancellation. Cleanup failure is logged and swallowed — a leftover
    directory is disk debris for hygiene to reap, never a reason to fail a
    call that already ran.
    """
    _AMBIENT_JUDGE_ROOT.mkdir(parents=True, exist_ok=True)
    # mkdir(exist_ok=True) ACCEPTS a symlink-to-directory — is_dir() follows
    # links — which would silently relocate every judge cwd under a path
    # somebody else chose. That is the same shape as the symlink that walked
    # past the sweep this design replaced, one level up. O_NOFOLLOW is the
    # check that cannot be faked; failure propagates into the caller's status
    # dict rather than running a judge from an unverified location.
    fd = os.open(
        _AMBIENT_JUDGE_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    os.close(fd)
    path = tempfile.mkdtemp(prefix="judge-", dir=_AMBIENT_JUDGE_ROOT)
    try:
        yield path
    finally:
        try:
            shutil.rmtree(path)
        except OSError:
            logger.warning("ambient-judge dir %s not removed", path, exc_info=True)


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
    # The child runs from a per-call judge dir (not the parent's cwd), so any
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
        with _judge_cwd() as judge_cwd:
            return await _run_in_cwd(
                prompt,
                model=model,
                claude_path=claude_path,
                no_mcp_config=no_mcp_config,
                timeout_s=timeout_s,
                cwd=judge_cwd,
            )
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


async def _run_in_cwd(
    prompt: str,
    *,
    model: str,
    claude_path: str,
    no_mcp_config: str | None,
    timeout_s: float,
    cwd: str,
) -> dict:
    """The spawn itself, in a caller-owned cwd (see ``_judge_cwd``)."""
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
            # A fresh per-call directory outside any git repo (see
            # _judge_cwd): the transcript lands under a non-interactive
            # project key so CC's resume picker never lists these one-turn
            # judgments, the repo's SessionStart hooks don't inject
            # context into them, and nothing can have planted context in a
            # directory that did not exist a moment ago. Measured
            # 2026-09-04: without this, arbiter/ledger/repo-pulse
            # transcripts accumulated in the interactive project dir and
            # surfaced in /resume.
            cwd=cwd,
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
