"""Tests for proactive-hook procedure tier gating (Surfacing v2 PR-A).

The UserPromptSubmit hook's `_search_procedures` previously surfaced the
best cosine match across ALL tiers — including the large pool of unproven
DORMANT (L4) drafts. v2 gates proactive surfacing to LIBRARY+ (L1/L2/L3),
making DORMANT drafts recall-only (never auto-injected).

`_search_procedures(db_path, prompt_vector)` takes the vector directly, so
these are fast unit tests with seeded embeddings — no embedding backend.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.learning.procedural.embedding import pack_embedding

# Load the hook script as a module (scripts/ is not a package). Pop the
# CC-session guard first so the module's top-level `sys.exit(0)` never fires.
_HOOK_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "proactive_memory_hook.py"
os.environ.pop("GENESIS_CC_SESSION", None)
_spec = importlib.util.spec_from_file_location("proactive_memory_hook", _HOOK_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["proactive_memory_hook"] = _mod
_spec.loader.exec_module(_mod)

_search_procedures = _mod._search_procedures


def _vec(*head: float) -> list[float]:
    """A 1024-dim embedding: leading components then zero-pad (pack_embedding
    requires EMBEDDING_DIM=1024). Cosine is determined by the head values."""
    return list(head) + [0.0] * (1024 - len(head))


_COLS = (
    "id, task_type, principle, steps, tools_used, context_tags, "
    "confidence, created_at, activation_tier, principle_embedding"
)


async def _seed(db_path, rows: list[dict]) -> None:
    async with aiosqlite.connect(str(db_path)) as conn:
        await create_all_tables(conn)
        for r in rows:
            await conn.execute(
                f"INSERT INTO procedural_memory ({_COLS}) "
                "VALUES (?, ?, ?, '[]', '[]', '[]', ?, ?, ?, ?)",
                (
                    r["id"], r["task_type"], r["principle"], r["confidence"],
                    "2026-01-01T00:00:00", r["activation_tier"],
                    pack_embedding(r["embedding"]),
                ),
            )
        await conn.commit()


@pytest.mark.asyncio
async def test_dormant_excluded_even_at_best_cosine(tmp_path):
    """A DORMANT (L4) row that is the BEST cosine match is still excluded;
    the lower-scoring LIBRARY (L3) row is surfaced instead."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "d", "task_type": "dorm", "principle": "dormant", "confidence": 0.9,
         "activation_tier": "L4", "embedding": _vec(1.0)},
        {"id": "l", "task_type": "lib", "principle": "library", "confidence": 0.5,
         "activation_tier": "L3", "embedding": _vec(0.8, 0.6)},
    ])
    result = _search_procedures(db, _vec(1.0))
    assert result is not None
    _proc_id, task_type, _principle = result
    assert task_type == "lib"


@pytest.mark.asyncio
async def test_only_dormant_returns_none(tmp_path):
    """When the only match is DORMANT, nothing is proactively surfaced."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "d", "task_type": "dorm", "principle": "p", "confidence": 0.9,
         "activation_tier": "L4", "embedding": _vec(1.0)},
    ])
    assert _search_procedures(db, _vec(1.0)) is None


@pytest.mark.asyncio
async def test_library_tier_still_surfaced(tmp_path):
    """Regression: LIBRARY+ tiers are NOT over-filtered — a clean match still surfaces."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "l", "task_type": "lib", "principle": "p", "confidence": 0.5,
         "activation_tier": "L3", "embedding": _vec(1.0)},
    ])
    result = _search_procedures(db, _vec(1.0))
    assert result is not None and result[1] == "lib"
