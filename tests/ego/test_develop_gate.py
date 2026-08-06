"""Develop-boundary gate + global cap + deliverable floor (proposal hygiene).

Covers:
- ``EgoSession._flag_develop_scope`` — the deterministic operate-vs-develop
  floor behind ``genesis_self_development_enabled`` (roadmap flag)
- ``_DEVELOP_MARKER_PATTERNS`` — action-phrasing precision incl. the
  cause-mention false-positive class
- ``EgoConfig.genesis_self_development_enabled`` default + validation
- the global (cross-ego) pending cap in ``_process_proposals``
- ``create_batch`` deliverable-floor injection (genesis ego only) + tabled-dedup
- ``_required_outputs_block`` shared renderer

Wall-clock-independent; no live services.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.config import validate_ego_config
from genesis.ego.proposals import ProposalWorkflow
from genesis.ego.session import EgoSession, _required_outputs_block
from genesis.ego.types import EgoConfig
from genesis.ego.verification import parse_expected_outputs


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        yield conn


def _gate_session():
    """Minimal mock EgoSession with the real gate method bound."""
    session = MagicMock()
    session._source_tag = "genesis_ego_cycle"
    session._flag_develop_scope = EgoSession._flag_develop_scope.__get__(session)
    session._DEVELOP_MARKER_PATTERNS = EgoSession._DEVELOP_MARKER_PATTERNS
    return session


# ── flag + gate behavior ─────────────────────────────────────────────────


class TestFlagDevelopScope:
    def test_pr_review_marked(self, monkeypatch):
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: EgoConfig(genesis_self_development_enabled=False),
        )
        proposals = [
            {
                "action_type": "investigate",
                "content": "Dispatch a session to review GENesis-AGI PR #1278 "
                "for remaining fail-open edges before merge.",
            }
        ]
        out = _gate_session()._flag_develop_scope(proposals)
        assert out[0].get("_develop_scope"), "PR-review proposal must be marked"

    def test_symptom_diagnosis_not_marked(self, monkeypatch):
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: EgoConfig(genesis_self_development_enabled=False),
        )
        proposals = [
            {
                "action_type": "investigate",
                "content": "Read-only diagnosis of why user_model_delta "
                "emissions have been silent 14+ days. Check the "
                "MIN_DELTA_CONFIDENCE gate against actual output confidence.",
            },
            {
                "action_type": "maintenance",
                "content": "Restart the wedged genesis-server service and "
                "clear the dead-letter queue.",
            },
        ]
        out = _gate_session()._flag_develop_scope(proposals)
        assert not any(p.get("_develop_scope") for p in out)

    def test_cause_mention_not_marked(self, monkeypatch):
        """Naming a refactor/migration as the suspected CAUSE stays operate."""
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: EgoConfig(genesis_self_development_enabled=False),
        )
        proposals = [
            {
                "action_type": "investigate",
                "content": "Diagnose why backups fail since the scheduler "
                "refactor landed; escalate findings.",
            },
            {
                "action_type": "investigate",
                "content": "The schema migration failed on restart — "
                "read-only diagnosis, escalate to user.",
            },
        ]
        out = _gate_session()._flag_develop_scope(proposals)
        assert not any(p.get("_develop_scope") for p in out)

    def test_action_type_self_modify_marked(self, monkeypatch):
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: EgoConfig(genesis_self_development_enabled=False),
        )
        proposals = [{"action_type": "code_change", "content": "Adjust the threshold."}]
        out = _gate_session()._flag_develop_scope(proposals)
        assert out[0].get("_develop_scope", "").startswith("action domain")

    def test_flag_on_disables_gate(self, monkeypatch):
        """The self-development unlock: flag True → nothing is marked."""
        monkeypatch.setattr(
            "genesis.ego.config.load_ego_config",
            lambda: EgoConfig(genesis_self_development_enabled=True),
        )
        proposals = [
            {
                "action_type": "code_change",
                "content": "Refactor the routing module.",
            }
        ]
        out = _gate_session()._flag_develop_scope(proposals)
        assert not any(p.get("_develop_scope") for p in out)

    def test_config_failure_enforces(self, monkeypatch):
        """Config read failure fails toward LESS autonomy (gate stays on)."""

        def _boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr("genesis.ego.config.load_ego_config", _boom)
        proposals = [
            {
                "action_type": "investigate",
                "content": "Review the open pull request on the public repo.",
            }
        ]
        out = _gate_session()._flag_develop_scope(proposals)
        assert out[0].get("_develop_scope")


class TestConfigFlag:
    def test_default_off(self):
        assert EgoConfig().genesis_self_development_enabled is False

    def test_validator(self):
        assert validate_ego_config({"genesis_self_development_enabled": True}) == []
        errs = validate_ego_config({"genesis_self_development_enabled": "yes"})
        assert any("genesis_self_development_enabled" in e for e in errs)


# ── global cap ───────────────────────────────────────────────────────────


def _cap_session(db, max_pending=15):
    session = MagicMock()
    session._source_tag = "genesis_ego_cycle"
    session._db = db
    session._config = MagicMock()
    session._config.max_pending_proposals = max_pending
    session._proposals = MagicMock()
    session._proposals.create_batch = AsyncMock(return_value=("batch", [], []))
    session._proposals.validate_batch = AsyncMock(return_value=[])
    session._proposals.send_digest = AsyncMock(return_value=None)
    session._event_bus = None
    session._process_proposals = EgoSession._process_proposals.__get__(session)
    return session


async def _seed_pending(db, *, id, ego_source, age_hours=48, rank=None):
    created = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type="investigate",
        content=f"proposal {id}",
        created_at=created,
        rank=rank,
        ego_source=ego_source,
    )


class TestGlobalCap:
    @pytest.mark.asyncio
    async def test_cap_counts_both_egos(self, db):
        """16 pending across BOTH egos + 1 incoming → global-oldest tabled."""
        for i in range(9):
            await _seed_pending(db, id=f"u{i}", ego_source="user_ego_cycle", age_hours=200 - i)
        for i in range(7):
            await _seed_pending(db, id=f"g{i}", ego_source="genesis_ego_cycle", age_hours=100 - i)
        session = _cap_session(db)
        await session._process_proposals(
            [{"action_type": "investigate", "content": "new one"}], "cycle-1"
        )
        cur = await db.execute("SELECT COUNT(*) FROM ego_proposals WHERE status = 'pending'")
        remaining = (await cur.fetchone())[0]
        # 16 pending + 1 incoming = 17 → excess 2 tabled (oldest, cross-ego)
        assert remaining == 14
        cur = await db.execute(
            "SELECT id FROM ego_proposals WHERE status = 'tabled' ORDER BY created_at"
        )
        tabled = [r[0] for r in await cur.fetchall()]
        assert tabled == ["u0", "u1"], "global-oldest evicted regardless of ego"

    @pytest.mark.asyncio
    async def test_develop_flagged_incoming_not_counted(self, db):
        """A flagged draft is created-then-tabled — it must not evict others."""
        for i in range(15):
            await _seed_pending(db, id=f"p{i}", ego_source="user_ego_cycle", age_hours=200 - i)
        session = _cap_session(db)
        await session._process_proposals(
            [
                {
                    "action_type": "investigate",
                    "content": "review pull request",
                    "_develop_scope": "develop marker",
                }
            ],
            "cycle-1",
        )
        cur = await db.execute("SELECT COUNT(*) FROM ego_proposals WHERE status = 'pending'")
        # _incoming == 0 → 15 + 0 is not > 15 → nothing evicted
        assert (await cur.fetchone())[0] == 15


# ── deliverable floor + tabled dedup (create_batch) ──────────────────────


class TestCreateBatchHygiene:
    @pytest.mark.asyncio
    async def test_injects_default_expected_outputs_genesis_ego(self, db):
        wf = ProposalWorkflow(db=db, topic_manager=None)
        _, ids, _ = await wf.create_batch(
            [{"action_type": "investigate", "content": "probe the breakers"}],
            ego_source="genesis_ego_cycle",
        )
        row = await ego_crud.get_proposal(db, ids[0])
        parsed = parse_expected_outputs(row["expected_outputs"])
        assert parsed is not None, "default expected_outputs must round-trip"
        assert parsed.files == [f"~/.genesis/output/ego-reports/{ids[0]}.md"]

    @pytest.mark.asyncio
    async def test_no_injection_for_user_ego(self, db):
        wf = ProposalWorkflow(db=db, topic_manager=None)
        _, ids, _ = await wf.create_batch(
            [{"action_type": "dispatch", "content": "publish the article"}],
            ego_source="user_ego_cycle",
        )
        row = await ego_crud.get_proposal(db, ids[0])
        assert row["expected_outputs"] is None

    @pytest.mark.asyncio
    async def test_ego_supplied_outputs_untouched(self, db):
        wf = ProposalWorkflow(db=db, topic_manager=None)
        eo = {"files": ["~/x.md"], "min_size_bytes": 100}
        _, ids, _ = await wf.create_batch(
            [
                {
                    "action_type": "investigate",
                    "content": "probe things",
                    "expected_outputs": eo,
                }
            ],
            ego_source="genesis_ego_cycle",
        )
        row = await ego_crud.get_proposal(db, ids[0])
        assert json.loads(row["expected_outputs"]) == eo

    @pytest.mark.asyncio
    async def test_flagged_draft_dedups_against_tabled(self, db):
        """A tabled develop twin blocks re-creation (no per-cycle churn)."""
        wf = ProposalWorkflow(db=db, topic_manager=None)
        draft = {
            "action_type": "investigate",
            "content": "review pull request #9",
            "_develop_scope": "develop marker",
        }
        _, ids, _ = await wf.create_batch([dict(draft)], ego_source="genesis_ego_cycle")
        assert len(ids) == 1
        await ego_crud.table_proposal(db, ids[0])
        _, ids2, _ = await wf.create_batch([dict(draft)], ego_source="genesis_ego_cycle")
        assert ids2 == [], "tabled twin must block re-creation of flagged draft"

    @pytest.mark.asyncio
    async def test_unflagged_draft_ignores_tabled(self, db):
        """Normal drafts keep the original pending/approved-only dedup."""
        wf = ProposalWorkflow(db=db, topic_manager=None)
        draft = {"action_type": "maintenance", "content": "clear the queue"}
        _, ids, _ = await wf.create_batch([dict(draft)], ego_source="genesis_ego_cycle")
        await ego_crud.table_proposal(db, ids[0])
        _, ids2, _ = await wf.create_batch([dict(draft)], ego_source="genesis_ego_cycle")
        assert len(ids2) == 1, "tabled rows never block ordinary re-proposal"


# ── shared required-outputs renderer ─────────────────────────────────────


class TestRequiredOutputsBlock:
    def test_renders_files_and_size(self):
        block = _required_outputs_block(json.dumps({"files": ["~/a.md"], "min_size_bytes": 500}))
        assert "Required Output Files" in block
        assert "~/a.md" in block
        assert "500 bytes" in block

    def test_empty_inputs(self):
        assert _required_outputs_block(None) == ""
        assert _required_outputs_block("") == ""
        assert _required_outputs_block("not json") == ""
        assert _required_outputs_block(json.dumps({"files": []})) == ""
