"""GitNexus jobs — code-graph reindex and CLAUDE.md block hygiene.

Bodies extracted verbatim from ``SurplusScheduler``; the scheduler keeps both
method names as thin delegates (and re-exports ``_strip_gitnexus_block`` for
its test seam). These jobs read no scheduler state — every dependency comes
from ``GenesisRuntime.instance()`` or the filesystem — so they take no
arguments. Function-scope imports are intentional; do not hoist them to
module top.
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# `gitnexus analyze` injects a `<!-- gitnexus:start --> … <!-- gitnexus:end -->`
# block into BOTH CLAUDE.md and AGENTS.md, with no per-file flag. We keep it in
# AGENTS.md (read by cross-tool agents — Codex/Cursor/etc.) but strip it from
# CLAUDE.md so Claude Code's instructions file stays clean.
_GITNEXUS_BLOCK_RE = re.compile(
    r"\n*<!-- gitnexus:start -->.*?<!-- gitnexus:end -->[^\n]*\n?",
    re.DOTALL,
)


def _strip_gitnexus_block(path: Path) -> bool:
    """Remove GitNexus's auto-injected block from a file. Returns True if removed."""
    try:
        text = path.read_text()
    except OSError:
        return False
    stripped = _GITNEXUS_BLOCK_RE.sub("", text)
    if stripped == text:
        return False
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    path.write_text(stripped)
    return True


async def run_gitnexus_reindex() -> None:
    """Reindex the GitNexus code graph (Mon & Thu 5am UTC).

    Runs the GitNexus reindex as a subprocess via the locked index
    entrypoint. CPU-only AST parsing, no ONNX/GPU (embeddings off by
    default). Incremental since GitNexus 1.6.5 — fast on unchanged repos.
    """
    import asyncio
    import shutil

    try:
        from genesis.runtime import GenesisRuntime
        if GenesisRuntime.instance().paused:
            logger.debug("GitNexus reindex skipped (Genesis paused)")
            return
    except Exception:
        logger.warning("Pause check failed — skipping GitNexus reindex", exc_info=True)
        return

    gitnexus = shutil.which("gitnexus")
    if not gitnexus:
        logger.warning("GitNexus reindex skipped — gitnexus not found on PATH")
        return

    try:
        repo_root = str(Path.home() / "genesis")
        # Route through the single locked+capped index entrypoint.
        # max_instances=1 only dedups within THIS process; the
        # post-commit hook and bootstrap spawn indexers out-of-process,
        # so the entrypoint's flock is the cross-process single-flight
        # guard (and its systemd scope caps memory/IO/CPU). No bare
        # binary fallback — raw indexer spawns are banned (guardrail
        # test), and the entrypoint ships with the repo.
        entrypoint = Path(repo_root) / "scripts" / "lib" / "code_intel_index.sh"
        if not entrypoint.is_file():
            logger.warning(
                "GitNexus reindex skipped — index entrypoint missing at %s",
                entrypoint,
            )
            return
        proc = await asyncio.create_subprocess_exec(
            "bash", str(entrypoint), repo_root, "gitnexus",
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group: proc is a bash wrapper — the actual
            # indexer is its (grand)child, so a timeout kill must hit
            # the whole group or it orphans the indexer (which keeps
            # running AND holds the stdout pipe, blocking communicate()
            # forever and wedging the max_instances=1 slot for good).
            start_new_session=True,
        )
        try:
            # 2-hour cap: gitnexus analyze is AST-only (no ONNX), but a
            # cold full-repo pass can take minutes. Hanging indefinitely
            # blocks the job slot (max_instances=1) forever.
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=7200
            )
        except TimeoutError:
            import os
            import signal
            # pgid > 1 guard: killpg(1) == kill ALL user processes
            # (and mocked procs default to pid 1 in tests).
            if isinstance(proc.pid, int) and proc.pid > 1:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            # Bounded drain: after a group SIGKILL this returns at once;
            # the bound only guards against a pipe-holder that escaped
            # the group (e.g. via its own setsid) re-wedging the slot.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.communicate(), timeout=30)
            logger.error("GitNexus reindex timed out after 2h — killed")
            with contextlib.suppress(Exception):
                GenesisRuntime.instance().record_job_failure(
                    "gitnexus_reindex", "timed out after 2h",
                )
            return
        out_text = (stdout or b"").decode(errors="replace")
        if proc.returncode == 0 and (
            "skip:" in out_text or "disabled via" in out_text
        ):
            # Entrypoint no-op (lock held by a concurrent index, or
            # indexing disabled) — do NOT claim a reindex happened.
            logger.info(
                "GitNexus reindex skipped by entrypoint: %s",
                out_text.strip()[:200],
            )
            with contextlib.suppress(Exception):
                GenesisRuntime.instance().record_job_success("gitnexus_reindex")
            return
        if proc.returncode == 0:
            logger.info("GitNexus reindex complete")
            # Keep the gitnexus block in AGENTS.md (cross-tool agents) but
            # strip it from CLAUDE.md — analyze injects both with no per-file flag.
            with contextlib.suppress(Exception):
                if _strip_gitnexus_block(Path(repo_root) / "CLAUDE.md"):
                    logger.info("Stripped GitNexus block from CLAUDE.md (kept in AGENTS.md)")
            with contextlib.suppress(Exception):
                GenesisRuntime.instance().record_job_success("gitnexus_reindex")
        else:
            err_msg = (stderr or stdout or b"unknown error").decode()[:200]
            logger.error("GitNexus reindex failed (rc=%d): %s", proc.returncode, err_msg)
            with contextlib.suppress(Exception):
                GenesisRuntime.instance().record_job_failure(
                    "gitnexus_reindex", err_msg,
                )
    except Exception as exc:
        logger.exception("GitNexus reindex failed")
        with contextlib.suppress(Exception):
            GenesisRuntime.instance().record_job_failure(
                "gitnexus_reindex", str(exc),
            )


async def run_gitnexus_strip() -> None:
    """Strip GitNexus's auto-injected block from CLAUDE.md (hourly + on startup).

    ``gitnexus analyze`` re-injects the block into CLAUDE.md on EVERY reindex,
    including the out-of-band staleness reindex run by GitNexus's own MCP
    server — which never triggers ``run_gitnexus_reindex``'s post-strip. This
    decoupled job keeps CLAUDE.md clean regardless of what reindexed; AGENTS.md
    intentionally keeps the block (read by cross-tool agents). Idempotent no-op
    when the block is absent.
    """
    from genesis.runtime import GenesisRuntime

    try:
        if _strip_gitnexus_block(Path.home() / "genesis" / "CLAUDE.md"):
            logger.info("Stripped GitNexus block from CLAUDE.md (kept in AGENTS.md)")
        with contextlib.suppress(Exception):
            GenesisRuntime.instance().record_job_success("gitnexus_strip")
    except Exception as exc:
        logger.warning("GitNexus strip failed", exc_info=True)
        with contextlib.suppress(Exception):
            GenesisRuntime.instance().record_job_failure("gitnexus_strip", str(exc))
