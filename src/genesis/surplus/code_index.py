"""CodeIndexExecutor — surplus executor for periodic codebase indexing."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from genesis.surplus.types import ExecutorResult, SurplusTask

logger = logging.getLogger(__name__)


class CodeIndexExecutor:
    """Run the AST codebase indexer as a surplus task.

    No LLM calls — pure AST parsing. Cheap enough for LOCAL_30B tier.
    """

    def __init__(self, *, db: aiosqlite.Connection, repo_root: Path | None = None):
        self._db = db
        self._repo_root = repo_root or Path(__file__).resolve().parents[3]  # -> genesis/

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        from genesis.codebase.indexer import index_codebase

        try:
            result = await index_codebase(self._db, self._repo_root)
            return ExecutorResult(
                success=True,
                content=(
                    f"Indexed {result.modules_indexed} modules "
                    f"({result.modules_unchanged} unchanged, {result.modules_skipped} skipped). "
                    f"{result.total_symbols} symbols, {result.total_imports} imports."
                ),
            )
        except Exception as exc:
            logger.error("Code index failed: %s", exc, exc_info=True)
            return ExecutorResult(success=False, error=str(exc))
