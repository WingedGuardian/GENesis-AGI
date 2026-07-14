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

from genesis.surplus.jobs._guard import record_failure, record_success

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
    """Queue a GitNexus reindex request (Mon & Thu 5am UTC) for the idle runner.

    Historically this spawned ``gitnexus analyze`` via the locked entrypoint
    directly. That is now the idle-gated runner's job — this scheduled job just
    drops an index-request marker (scripts/lib/index_marker.py), so a 5am
    reindex can never itself storm the container (the second index-storm was a
    scheduled/triggered full index hitting a wiped DB). Success here means
    "marker enqueued"; the actual index outcome lands in the runner's journal.
    Kept ``async`` because the scheduler awaits it.
    """
    try:
        from genesis.runtime import GenesisRuntime
        if GenesisRuntime.instance().paused:
            logger.debug("GitNexus reindex request skipped (Genesis paused)")
            return
    except Exception:
        logger.warning("Pause check failed — skipping GitNexus reindex request", exc_info=True)
        return

    repo_root = Path.home() / "genesis"
    if (repo_root / ".git").is_file():
        logger.debug("GitNexus reindex request skipped — %s is a worktree", repo_root)
        return
    marker_helper = repo_root / "scripts" / "lib" / "index_marker.py"
    if not marker_helper.is_file():
        logger.warning(
            "GitNexus reindex request skipped — marker helper missing at %s",
            marker_helper,
        )
        return
    try:
        import sys

        lib = str((repo_root / "scripts" / "lib").resolve())
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import index_marker  # stdlib-only

        index_marker.write_marker(str(repo_root), tools="gitnexus", mode="fast")
        logger.info("GitNexus reindex request queued for the idle runner")
        with contextlib.suppress(Exception):
            GenesisRuntime.instance().record_job_success("gitnexus_reindex")
    except Exception as exc:
        logger.exception("GitNexus reindex request failed")
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
    try:
        if _strip_gitnexus_block(Path.home() / "genesis" / "CLAUDE.md"):
            logger.info("Stripped GitNexus block from CLAUDE.md (kept in AGENTS.md)")
        record_success("gitnexus_strip")
    except Exception as exc:
        logger.warning("GitNexus strip failed", exc_info=True)
        record_failure("gitnexus_strip", str(exc))
