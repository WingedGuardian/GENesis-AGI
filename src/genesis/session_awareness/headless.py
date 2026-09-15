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
from collections.abc import Iterator
from pathlib import Path

from genesis.util.proc_kill import kill_process_group, reap_bounded

logger = logging.getLogger(__name__)

# ONE stable working directory for every ambient judge, and customization
# suppression as the actual isolation.
#
# The judge child must not inherit CONTEXT it did not author. CC reads
# CLAUDE.md / CLAUDE.local.md / .mcp.json from its cwd, reads memory files
# from every ANCESTOR of that cwd, and always loads the user-level
# ~/.claude/CLAUDE.md. Tool denial does not stop instruction poisoning, and
# dispatched background sessions (research/interact/campaign) hold Write with
# no path scope, so they can plant a memory file these judges would read.
#
# An earlier design answered that with a FRESH per-call directory: nothing can
# pre-plant in a directory whose name did not exist a moment ago. Three review
# rounds went into it, and it was still the wrong shape — it defends the LEAF
# while the poisonable surfaces are the ANCESTORS, which a fresh leaf does not
# make fresh. Its own comment conceded that boundary.
#
# The control is now `--safe-mode` in build_argv, which disables CLAUDE.md,
# skills, plugins, hooks, MCP servers and custom commands outright while
# keeping OAuth — the only OAuth-compatible way to suppress the user-level
# memory file (cc/types.py:347-353). That closes the ancestor class the fresh
# directory could not, and it closes it for the cwd too.
#
# With suppression doing the work, per-call freshness buys nothing and COSTS:
# CC derives a Claude project identity from the cwd, so every distinct cwd
# creates its own tree under ~/.claude/projects/ that the cwd's own cleanup
# does not touch (cc/types.py:481-492). A per-call directory therefore leaked
# one project tree per judgment, unbounded across the ledger backfill loop.
# One stable directory means one project tree, reused (Codex P1 + P2, #1693).
#
# Still out of any git repo, so CC's resume picker never lists these one-turn
# judgments beside interactive sessions.


def _ambient_judge_dir() -> Path:
    """The ONE stable working directory shared by every ambient judge call.

    Resolved through ``genesis_home()`` rather than a hardcoded
    ``~/.genesis``, so a relocated install (read-only home, state on another
    volume) does not silently write outside itself and fail every judgment.
    Resolved at CALL time, never frozen at import, because GENESIS_HOME is
    read from the environment (Codex P2, PR #1693).
    """
    from genesis.env import genesis_home

    return genesis_home() / "ambient-judges" / "judge"



@contextlib.contextmanager
def _judge_cwd() -> Iterator[str]:
    """A fresh, private working directory for one judge call.

    Yields the path; removes it on exit, including after a timeout or a
    cancellation. Cleanup failure is logged and swallowed — a leftover
    directory is disk debris for hygiene to reap, never a reason to fail a
    call that already ran.
    """
    path = _ambient_judge_dir()
    path.mkdir(parents=True, exist_ok=True)
    # mkdir(exist_ok=True) ACCEPTS a symlink-to-directory — is_dir() follows
    # links — which would silently relocate every judge cwd under a path
    # somebody else chose. O_NOFOLLOW is the check that cannot be faked;
    # failure propagates into the caller's status dict rather than running a
    # judge from an unverified location.
    os.close(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
    # Nothing is removed. The directory is STABLE and deliberately reused, so
    # there is nothing here to clean up per call.
    yield str(path)


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
        # --safe-mode: the CUSTOMIZATION half of the isolation, and the part a
        # fresh working directory could never provide. CC reads memory files
        # from every ANCESTOR of the cwd and always loads the user-level
        # ~/.claude/CLAUDE.md, which it finds through the passwd-resolved home
        # regardless of $HOME or $CLAUDE_CONFIG_DIR (probe-verified
        # 2026-07-09, cc/types.py:347-353). So any write-enabled session that
        # can drop a memory file in a parent directory poisons every later
        # judge, and no cwd discipline closes that — the directories ABOVE a
        # fresh directory are not themselves fresh.
        #
        # safe-mode disables CLAUDE.md, skills, plugins, hooks, MCP servers
        # and custom commands/agents while leaving OAuth intact, which --bare
        # does not (it refuses OAuth and demands an API key). These judges read
        # EXTERNAL text — PR titles and bodies via repo-pulse — so instruction
        # suppression is the control that matters: tool denial stops execution
        # but not a fabricated arbiter, ledger or repo-pulse verdict.
        # Precedent in this repo: the eval bench's bare arm (eval/bench/arms.py)
        # and skill-replay both rely on it, and --system-prompt is honoured
        # under it (probe-verified 2026-07-20). (Codex P1, PR #1693.)
        "--safe-mode",
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
