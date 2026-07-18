"""Tests for cognitive_variant — the resolution hook that promotes an Evo-measured
reflection-prompt winner to the live overlay (Evo PR-B).

Recommend-only: the prompt is written ONLY on the user's explicit approval, via
the rollback-able cognitive ledger. The handler marks the proposal 'executed'
unconditionally (even if the write fails) so it never lingers 'approved' where
the dispatch sweep could pick it up.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

import genesis
from genesis.awareness.types import Depth
from genesis.cc.reflection_bridge._prompts import system_prompt_for_depth
from genesis.db.crud import ego as ego_crud
from genesis.db.schema import create_all_tables
from genesis.ego.cognitive_variant import (
    _OVERLAY_FILENAME,
    handle_cognitive_variant_resolution,
)

_WINNER = "# REFLECTION_DEEP (promoted)\n\nThink deeply. Be concise."


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def overlay_dir(tmp_path, monkeypatch):
    """Point the reflection overlay at a temp dir so tests never touch the real
    ~/.genesis/config/reflection."""
    monkeypatch.setenv("GENESIS_REFLECTION_PROMPT_DIR", str(tmp_path))
    return tmp_path


async def _make_proposal(
    db, *, status="approved", action_type="cognitive_variant_promotion",
    full_prompt=_WINNER, confidence=0.9, approach="more concise",
):
    pid = "cvp1"
    outputs = {"full_prompt": full_prompt, "approach": approach, "evidence": "p=0.01"}
    await ego_crud.create_proposal(
        db, id=pid, action_type=action_type, content="promote reflection variant",
        status="pending", created_at="2026-06-24T00:00:00+00:00",
        confidence=confidence, expected_outputs=json.dumps(outputs),
    )
    await ego_crud.resolve_proposal(db, pid, status=status)
    return await ego_crud.get_proposal(db, pid)


async def test_approved_writes_overlay_and_executes(db, overlay_dir):
    prop = await _make_proposal(db, status="approved")

    ok = await handle_cognitive_variant_resolution(db, prop, "approved")

    assert ok is True
    # overlay written with the winner prompt
    overlay = overlay_dir / _OVERLAY_FILENAME
    assert overlay.read_text() == _WINNER
    # proposal marked executed (never lingers 'approved')
    assert (await ego_crud.get_proposal(db, prop["id"]))["status"] == "executed"


async def test_overlay_target_matches_bridge_read(db, overlay_dir, tmp_path):
    """The file we WRITE must be the file the reflection bridge READS — guards
    against filename drift. After promotion, resolving DEEP returns the winner
    (overlay is checked before the repo dir)."""
    prop = await _make_proposal(db, status="approved")
    await handle_cognitive_variant_resolution(db, prop, "approved")

    empty_repo_dir = tmp_path / "repo_identity"
    empty_repo_dir.mkdir()
    assert system_prompt_for_depth(Depth.DEEP, empty_repo_dir) == _WINNER


async def test_rejected_is_noop(db, overlay_dir):
    prop = await _make_proposal(db, status="rejected")

    ok = await handle_cognitive_variant_resolution(db, prop, "rejected")

    assert ok is False
    assert not (overlay_dir / _OVERLAY_FILENAME).exists()
    # rejected proposals are left exactly as-is (recommend-only)
    assert (await ego_crud.get_proposal(db, prop["id"]))["status"] == "rejected"


async def test_non_cognitive_variant_is_noop(db, overlay_dir):
    prop = await _make_proposal(db, status="approved", action_type="autonomy_earnback")

    ok = await handle_cognitive_variant_resolution(db, prop, "approved")

    assert ok is False
    assert not (overlay_dir / _OVERLAY_FILENAME).exists()


async def test_below_confidence_floor_refused(db, overlay_dir):
    prop = await _make_proposal(db, status="approved", confidence=0.5)

    ok = await handle_cognitive_variant_resolution(db, prop, "approved")

    assert ok is False
    assert not (overlay_dir / _OVERLAY_FILENAME).exists()
    # marked executed so an unappliable approved proposal doesn't linger
    assert (await ego_crud.get_proposal(db, prop["id"]))["status"] == "executed"


async def test_missing_full_prompt_refused(db, overlay_dir):
    prop = await _make_proposal(db, status="approved", full_prompt="   ")

    ok = await handle_cognitive_variant_resolution(db, prop, "approved")

    assert ok is False
    assert not (overlay_dir / _OVERLAY_FILENAME).exists()
    assert (await ego_crud.get_proposal(db, prop["id"]))["status"] == "executed"


async def test_full_promote_approve_apply_rollback_e2e(db, overlay_dir, tmp_path):
    """Decisive E2E (real functions, no mocks for the apply path): an Evo winner
    is filed as a recommend-only proposal, the user approves it, the overlay is
    written + goes live to the reflection bridge, and rollback reverts it."""
    from genesis.experimentation.evo import EvoResult
    from genesis.experimentation.types import CognitiveVariant
    from genesis.learning import cognitive_ledger
    from genesis.mcp.health.evo_run import promote_evo_winner

    winner_prompt = "CANONICAL DEEP PROMPT\n\nbe more concise"
    result = EvoResult(
        winner=CognitiveVariant(
            name="evo_v0", description="be more concise", system_prompt=winner_prompt,
        ),
        winner_winrate={"recommendation": "treatment_wins", "p_value": 0.002},
        holdout_winrate={"recommendation": "treatment_wins", "p_value": 0.01,
                         "treatment_mean_score": 0.82, "control_mean_score": 0.70},
        candidates_evaluated=6, survivors=2, note="confirmed", holdout_disjoint=True,
    )
    overlay = overlay_dir / _OVERLAY_FILENAME

    # 1. Evo files the winner — recommend-only: NOTHING is applied yet.
    pid = await promote_evo_winner(
        db, result, gen_provider="cc-haiku", judge_provider="groq-free",
    )
    assert pid is not None
    assert not overlay.exists()

    # 2. The user approves → the resolution site resolves to 'approved' then
    #    runs the apply hook (mirrors all 4 wired resolution sites).
    await ego_crud.resolve_proposal(db, pid, status="approved")
    prop = await ego_crud.get_proposal(db, pid)
    assert await handle_cognitive_variant_resolution(db, prop, "approved") is True
    assert (await ego_crud.get_proposal(db, pid))["status"] == "executed"

    # 3. The overlay is written AND live (the bridge reads it ahead of the repo).
    assert overlay.read_text() == winner_prompt
    empty_repo = tmp_path / "repo_identity"
    empty_repo.mkdir()
    assert system_prompt_for_depth(Depth.DEEP, empty_repo) == winner_prompt

    # 4. Rollback reverts — overlay removed, no longer live.
    rows = await cognitive_ledger.recent(db, limit=1, actor="evo_promotion")
    assert rows, "ledger should have recorded the promotion"
    res = await cognitive_ledger.rollback(db, rows[0]["id"])
    assert res["ok"] is True
    assert not overlay.exists()
    assert system_prompt_for_depth(Depth.DEEP, empty_repo) != winner_prompt


def test_handler_wired_into_all_resolution_paths():
    """Every proposal-resolution entry point MUST call the cognitive-variant
    apply hook, or an approval there silently no-ops (the prompt is never
    promoted). Mirrors the cell_promotion / goal_status wiring guard."""
    root = Path(genesis.__file__).parent
    entry_points = [
        root / "ego" / "proposals.py",
        root / "mcp" / "health" / "ego_tools.py",
        root / "dashboard" / "routes" / "ego.py",
        root / "dashboard" / "routes" / "comms.py",
    ]
    for path in entry_points:
        src = path.read_text()
        assert "handle_proposal_resolution" in src, (
            f"{path} is missing the shared resolution hook — "
            "a resolution there would silently skip side effects"
        )
    shared = (root / "ego" / "resolution.py").read_text()
    assert "handle_cognitive_variant_resolution" in shared, (
        "ego/resolution.py no longer runs handle_cognitive_variant_resolution — "
        "that action would silently no-op on every entry point"
    )


def test_excluded_from_approved_proposal_sweep():
    """The promotion action_type must be in session.py's never-dispatch set,
    else an approved promotion could be auto-run as a session."""
    root = Path(genesis.__file__).parent
    src = (root / "ego" / "session.py").read_text()
    assert '"cognitive_variant_promotion"' in src


async def test_write_failure_still_marks_executed(db, overlay_dir, monkeypatch):
    """The architect's fail-open backstop: if the overlay write raises, the
    proposal must STILL be marked executed (returns False) — so it never lingers
    'approved' for the dispatch sweep to grab."""
    prop = await _make_proposal(db, status="approved")

    async def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(
        "genesis.learning.cognitive_ledger.record_file_modification", _boom,
    )

    ok = await handle_cognitive_variant_resolution(db, prop, "approved")

    assert ok is False  # write failed
    assert not (overlay_dir / _OVERLAY_FILENAME).exists()
    assert (await ego_crud.get_proposal(db, prop["id"]))["status"] == "executed"
