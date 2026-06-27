"""Tests for proactive-hook procedure tier gating (LC1-A: golden-dormant unlock).

History: "Surfacing v2" (PR-A) gated proactive surfacing to LIBRARY+ and made
DORMANT drafts recall-only — which locked golden-dormant drafts out forever
(never surfaced → never invoked → never promoted). LC1-A reverses that with a
SAFER rule: ALL tiers are eligible, but each row is gated against its OWN tier
threshold — DORMANT (unproven) must clear a STRICTER cosine bar
(`_DORMANT_SURFACE_THRESHOLD`, 0.78) than proven tiers
(`_PROCEDURE_SURFACE_THRESHOLD`, 0.70) — and the caller frames a surfaced
DORMANT row as an unproven suggestion. The highest-similarity row that clears
its own bar wins, so a high-cosine-but-rejected DORMANT never shadows a proven
LIBRARY match just below it.

`_search_procedures(db_path, prompt_vector)` takes the vector directly, so
these are fast unit tests with seeded embeddings — no embedding backend. It
returns a 4-tuple `(proc_id, task_type, principle_snippet, activation_tier)`.
"""

from __future__ import annotations

import importlib.util
import math
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


def _unit(cos: float) -> list[float]:
    """A 1024-dim UNIT vector whose cosine with ``_unit(1.0)`` (== [1,0,0,…]) is
    exactly ``cos``: components [cos, sqrt(1-cos^2), 0, …]."""
    rest = math.sqrt(max(0.0, 1.0 - cos * cos))
    return [cos, rest] + [0.0] * (1024 - 2)


_QUERY = _unit(1.0)  # cosine(_QUERY, _unit(c)) == c

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
async def test_dormant_surfaced_when_it_clears_the_strict_bar(tmp_path):
    """LC1-A: a DORMANT draft that clears the stricter 0.78 bar IS surfaced,
    and the returned 4-tuple carries its tier so the caller can frame it."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "d", "task_type": "dorm", "principle": "p", "confidence": 0.0,
         "activation_tier": "DORMANT", "embedding": _unit(0.9)},
    ])
    result = _search_procedures(db, _QUERY)
    assert result is not None
    proc_id, task_type, _principle, tier = result
    assert task_type == "dorm" and tier == "DORMANT"


@pytest.mark.asyncio
async def test_dormant_below_strict_bar_does_not_shadow_library(tmp_path):
    """The key safety property: a DORMANT with HIGHER cosine but below its own
    0.78 bar must NOT shadow a proven LIBRARY row that clears the 0.70 bar."""
    db = tmp_path / "t.db"
    await _seed(db, [
        # cosine 0.75: best overall, but below the DORMANT bar (0.78) → rejected
        {"id": "d", "task_type": "dorm", "principle": "p", "confidence": 0.9,
         "activation_tier": "DORMANT", "embedding": _unit(0.75)},
        # cosine 0.72: clears the LIBRARY bar (0.70) → should win
        {"id": "l", "task_type": "lib", "principle": "p", "confidence": 0.5,
         "activation_tier": "LIBRARY", "embedding": _unit(0.72)},
    ])
    result = _search_procedures(db, _QUERY)
    assert result is not None
    assert result[1] == "lib" and result[3] == "LIBRARY"


@pytest.mark.asyncio
async def test_only_dormant_below_bar_returns_none(tmp_path):
    """A DORMANT below the strict bar with no other candidate → nothing surfaces."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "d", "task_type": "dorm", "principle": "p", "confidence": 0.9,
         "activation_tier": "DORMANT", "embedding": _unit(0.75)},  # < 0.78
    ])
    assert _search_procedures(db, _QUERY) is None


@pytest.mark.asyncio
async def test_library_tier_still_surfaced(tmp_path):
    """Regression: proven LIBRARY+ tiers still surface on a clean match."""
    db = tmp_path / "t.db"
    await _seed(db, [
        {"id": "l", "task_type": "lib", "principle": "p", "confidence": 0.5,
         "activation_tier": "LIBRARY", "embedding": _unit(1.0)},
    ])
    result = _search_procedures(db, _QUERY)
    assert result is not None and result[1] == "lib" and result[3] == "LIBRARY"


@pytest.mark.asyncio
async def test_deprecated_and_quarantined_never_surface(tmp_path):
    """Correctness filters still apply — only DORMANT *exclusion* was lifted."""
    db = tmp_path / "t.db"
    async with aiosqlite.connect(str(db)) as conn:
        await create_all_tables(conn)
        for pid, tier, dep, quar in [("dep", "LIBRARY", 1, 0), ("quar", "CORE", 0, 1)]:
            await conn.execute(
                f"INSERT INTO procedural_memory ({_COLS}, deprecated, quarantined) "
                "VALUES (?, ?, ?, '[]', '[]', '[]', ?, ?, ?, ?, ?, ?)",
                (pid, "t", "p", 0.9, "2026-01-01T00:00:00", tier,
                 pack_embedding(_unit(1.0)), dep, quar),
            )
        await conn.commit()
    assert _search_procedures(db, _QUERY) is None
