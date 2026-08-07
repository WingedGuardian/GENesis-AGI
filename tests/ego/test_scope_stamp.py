"""Scope-stamp architecture — the operate-vs-develop boundary judged by the
realist/reconcile LLM (structured ``scope`` field) and enforced deterministically
at the create / revise / dispatch-claim chokepoints.

Replaces the deleted regex-marker gate. Wall-clock-independent; no live services.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.proposals import ProposalWorkflow, ensure_deliverable_spec
from genesis.ego.session import (
    EgoSession,
    _build_realist_prompt,
    _parse_realist_response,
    _parse_reconcile_response,
)
from genesis.ego.types import EgoConfig
from genesis.ego.verification import parse_expected_outputs


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for table in ("ego_proposals", "ego_proposal_revisions", "ego_state"):
            await conn.execute(TABLES[table])
        yield conn


def _patch_flag(monkeypatch, enabled: bool):
    monkeypatch.setattr(
        "genesis.ego.config.load_ego_config",
        lambda: EgoConfig(genesis_self_development_enabled=enabled),
    )


# ── realist scope contract ────────────────────────────────────────────────


class TestRealistScopeContract:
    def test_genesis_prompt_requests_scope(self):
        p = _build_realist_prompt(
            [{"content": "x", "action_type": "investigate"}],
            [],
            ego_source="genesis_ego_cycle",
        )
        assert '"scope": "operate|develop"' in p

    def test_user_prompt_omits_scope(self):
        p = _build_realist_prompt(
            [{"content": "x", "action_type": "outreach"}],
            [],
            ego_source="user_ego_cycle",
        )
        assert "scope" not in p.split("Output Format")[1]

    def test_parse_valid_scope(self):
        v = _parse_realist_response('[{"index":0,"verdict":"pass","scope":"develop"}]', 1)
        assert v[0]["scope"] == "develop"

    def test_parse_invalid_scope_dropped(self):
        v = _parse_realist_response('[{"index":0,"verdict":"pass","scope":"nonsense"}]', 1)
        assert "scope" not in v[0]

    def test_reconcile_parse_scope(self):
        v = _parse_reconcile_response(
            '[{"index":0,"verdict":"revise","target_id":"a","scope":"operate"}]', 1
        )
        assert v[0]["scope"] == "operate"

    def test_genesis_realist_sees_full_content_and_plan(self):
        """The scope judge must see the COMPLETE proposal (Codex P1): full
        content + execution_plan, not a 300-char clip."""
        long_content = "operate-looking preamble " * 20  # > 300 chars
        p = _build_realist_prompt(
            [
                {
                    "content": long_content + "THEN go refactor the router",
                    "action_type": "investigate",
                    "execution_plan": "edit src/genesis/routing/engine.py",
                }
            ],
            [],
            ego_source="genesis_ego_cycle",
        )
        assert "go refactor the router" in p  # past the old 300-char clip
        assert "execution_plan: edit src/genesis/routing/engine.py" in p

    def test_genesis_reconcile_sees_full_content_and_plan(self):
        from genesis.ego.session import _build_reconcile_prompt

        long_content = "sharpen preamble " * 20
        p = _build_reconcile_prompt(
            [
                {
                    "content": long_content + "THEN rewrite the module",
                    "action_type": "investigate",
                    "execution_plan": "patch src/genesis/x.py",
                }
            ],
            [],
            {"jobs": [], "stale_jobs": [], "merged_prs": []},
            ego_source="genesis_ego_cycle",
        )
        assert "rewrite the module" in p
        assert "execution_plan: patch src/genesis/x.py" in p


# ── SELF_MODIFY fast-path override in _filter_proposals apply-loop ─────────


class TestScopeFastPath:
    def _sess(self, db, text, *, is_error=False):
        sess = EgoSession.__new__(EgoSession)
        sess._source_tag = "genesis_ego_cycle"
        sess._db = db  # real conn: _filter_proposals fetches history from it
        sess._invoker = _FakeInvoker(text, is_error=is_error)
        sess._last_realist_cost_usd = 0.0
        return sess

    @pytest.mark.asyncio
    async def test_self_modify_action_forces_develop(self, db):
        """A code_change action-type is develop regardless of the LLM's scope."""
        sess = self._sess(db, '[{"index":0,"verdict":"pass","scope":"operate"}]')
        out = await sess._filter_proposals([{"content": "tweak it", "action_type": "code_change"}])
        assert out[0]["_realist_scope"] == "develop"

    @pytest.mark.asyncio
    async def test_llm_scope_used_when_no_fastpath(self, db):
        sess = self._sess(db, '[{"index":0,"verdict":"pass","scope":"develop"}]')
        out = await sess._filter_proposals(
            [{"content": "review the PR", "action_type": "investigate"}]
        )
        assert out[0]["_realist_scope"] == "develop"

    @pytest.mark.asyncio
    async def test_realist_error_leaves_no_scope(self, db):
        """Fail-closed: realist error → no _realist_scope → create drops it."""
        sess = self._sess(db, "", is_error=True)
        out = await sess._filter_proposals([{"content": "probe", "action_type": "investigate"}])
        assert "_realist_scope" not in out[0]


class _FakeInvoker:
    def __init__(self, text, *, is_error=False):
        self._text = text
        self._is_error = is_error

    async def run(self, invocation):
        class _R:
            pass

        r = _R()
        r.text = self._text
        r.is_error = self._is_error
        r.error_message = "boom" if self._is_error else ""
        r.cost_usd = 0.0
        return r


# ── create chokepoint ─────────────────────────────────────────────────────


async def _create(db, draft, ego_source, monkeypatch, *, flag=False):
    _patch_flag(monkeypatch, flag)
    wf = ProposalWorkflow(db=db, topic_manager=None)
    _, ids, created = await wf.create_batch([draft], ego_source=ego_source)
    return ids, created


class TestCreateChokepoint:
    @pytest.mark.asyncio
    async def test_unstamped_genesis_draft_dropped(self, db, monkeypatch):
        ids, _ = await _create(
            db,
            {"action_type": "investigate", "content": "no scope"},
            "genesis_ego_cycle",
            monkeypatch,
        )
        assert ids == []  # fail-closed drop
        cur = await db.execute("SELECT COUNT(*) FROM ego_proposals")
        assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_operate_persisted_pending(self, db, monkeypatch):
        ids, _ = await _create(
            db,
            {
                "action_type": "investigate",
                "content": "probe breakers",
                "_realist_scope": "operate",
            },
            "genesis_ego_cycle",
            monkeypatch,
        )
        assert len(ids) == 1
        row = await ego_crud.get_proposal(db, ids[0])
        assert row["status"] == "pending"
        assert row["scope"] == "operate"
        assert row["scope_revision"] == 1

    @pytest.mark.asyncio
    async def test_develop_flag_off_tabled(self, db, monkeypatch):
        ids, created = await _create(
            db,
            {"action_type": "investigate", "content": "review PR #9", "_realist_scope": "develop"},
            "genesis_ego_cycle",
            monkeypatch,
            flag=False,
        )
        assert len(ids) == 1
        assert created[0].get("_table_after_create") is True
        row = await ego_crud.get_proposal(db, ids[0])
        assert row["scope"] == "develop"
        # _process_proposals does the tabling; create leaves it pending but flagged.

    @pytest.mark.asyncio
    async def test_develop_flag_on_stays_pending(self, db, monkeypatch):
        ids, created = await _create(
            db,
            {
                "action_type": "investigate",
                "content": "refactor routing",
                "_realist_scope": "develop",
            },
            "genesis_ego_cycle",
            monkeypatch,
            flag=True,
        )
        assert len(ids) == 1
        assert not created[0].get("_table_after_create")
        row = await ego_crud.get_proposal(db, ids[0])
        assert row["status"] == "pending"
        assert row["scope"] == "develop"

    @pytest.mark.asyncio
    async def test_user_ego_never_scoped_never_dropped(self, db, monkeypatch):
        ids, _ = await _create(
            db,
            {"action_type": "outreach", "content": "email a lead"},
            "user_ego_cycle",
            monkeypatch,
        )
        assert len(ids) == 1  # NOT dropped despite no scope
        row = await ego_crud.get_proposal(db, ids[0])
        assert row["scope"] is None


# ── dispatch-claim guard ──────────────────────────────────────────────────


async def _seed_approved(db, *, id, scope):
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type="investigate",
        content=f"p {id}",
        status="approved",
        ego_source="genesis_ego_cycle",
        scope=scope,
        scope_revision=1 if scope else None,
    )


class TestClaimGuard:
    @pytest.mark.asyncio
    async def test_develop_unclaimable_when_disabled(self, db):
        await _seed_approved(db, id="d1", scope="develop")
        assert await ego_crud.claim_proposal_for_dispatch(db, "d1", allow_develop=False) is False
        assert (await ego_crud.get_proposal(db, "d1"))["status"] == "approved"

    @pytest.mark.asyncio
    async def test_develop_claimable_when_enabled(self, db):
        await _seed_approved(db, id="d2", scope="develop")
        assert await ego_crud.claim_proposal_for_dispatch(db, "d2", allow_develop=True) is True

    @pytest.mark.asyncio
    async def test_operate_claimable_when_disabled(self, db):
        await _seed_approved(db, id="o1", scope="operate")
        assert await ego_crud.claim_proposal_for_dispatch(db, "o1", allow_develop=False) is True

    @pytest.mark.asyncio
    async def test_null_scope_claimable(self, db):
        """User-ego / legacy rows (NULL scope) are never blocked by the guard."""
        await _seed_approved(db, id="n1", scope=None)
        assert await ego_crud.claim_proposal_for_dispatch(db, "n1", allow_develop=False) is True


# ── reconcile revise scope gate ───────────────────────────────────────────


def _revise_sess(db):
    s = EgoSession.__new__(EgoSession)
    s._db = db
    s._source_tag = "genesis_ego_cycle"
    return s


async def _pending(db, *, id, content, scope="operate"):
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type="investigate",
        content=content,
        status="pending",
        ego_source="genesis_ego_cycle",
        scope=scope,
        scope_revision=1,
    )


class TestReconcileReviseScope:
    @pytest.mark.asyncio
    async def test_operate_revise_applies_and_rescopes(self, db, monkeypatch):
        _patch_flag(monkeypatch, False)
        await _pending(db, id="rv1", content="stale")
        ok = await _revise_sess(db)._reconcile_revise(
            "rv1",
            {"id": "rv1", "revision_num": 1, "urgency": "normal", "action_type": "investigate"},
            {"content": "sharper"},
            "sharper",
            scope="operate",
        )
        assert ok is True
        row = await ego_crud.get_proposal(db, "rv1")
        assert row["content"] == "sharper"
        assert row["scope"] == "operate"
        assert row["scope_revision"] == 2

    @pytest.mark.asyncio
    async def test_missing_scope_not_applied(self, db, monkeypatch):
        _patch_flag(monkeypatch, False)
        await _pending(db, id="rv2", content="stale")
        ok = await _revise_sess(db)._reconcile_revise(
            "rv2",
            {"id": "rv2", "revision_num": 1, "urgency": "normal", "action_type": "investigate"},
            {"content": "sharper"},
            "sharper",
            scope=None,
        )
        assert ok is False  # fail-closed → kept as survivor
        assert (await ego_crud.get_proposal(db, "rv2"))["content"] == "stale"

    @pytest.mark.asyncio
    async def test_develop_revise_blocked_when_disabled(self, db, monkeypatch):
        _patch_flag(monkeypatch, False)
        await _pending(db, id="rv3", content="stale operate item")
        ok = await _revise_sess(db)._reconcile_revise(
            "rv3",
            {"id": "rv3", "revision_num": 1, "urgency": "normal", "action_type": "investigate"},
            {"content": "now go refactor the module"},
            "r",
            scope="develop",
        )
        assert ok is False  # operate item NOT mutated into develop
        assert (await ego_crud.get_proposal(db, "rv3"))["content"] == "stale operate item"

    @pytest.mark.asyncio
    async def test_retained_develop_plan_blocks_operate_downgrade(self, db, monkeypatch):
        """Codex round-2 P1: revise_proposal COALESCEs execution_plan, so a
        develop row revised by an operate draft that omits the plan RETAINS the
        develop plan. The merged row must be scoped develop (fast-path on the
        effective plan + no-silent-downgrade), so the revise is refused, not
        applied with an operate stamp over a code-edit plan."""
        _patch_flag(monkeypatch, False)
        await ego_crud.create_proposal(
            db,
            id="rv5",
            action_type="investigate",
            content="v1 develop",
            status="pending",
            ego_source="genesis_ego_cycle",
            scope="develop",
            scope_revision=1,
            execution_plan="edit src/genesis/routing/engine.py",
        )
        ok = await _revise_sess(db)._reconcile_revise(
            "rv5",
            {
                "id": "rv5",
                "revision_num": 1,
                "urgency": "normal",
                "action_type": "investigate",
                "scope": "develop",
                "execution_plan": "edit src/genesis/routing/engine.py",
            },
            {"content": "looks operate now"},  # draft omits execution_plan
            "r",
            scope="operate",  # LLM tried to downgrade
        )
        assert ok is False  # merged row is develop → not applied
        assert (await ego_crud.get_proposal(db, "rv5"))["content"] == "v1 develop"


# ── deliverable-spec parity + revision audit ──────────────────────────────


class TestGlobalCapWithScope:
    @pytest.mark.asyncio
    async def test_operate_incoming_counts_toward_cap(self, db, monkeypatch):
        """A genesis-ego operate draft counts toward the total-15 cap and can
        evict the global-oldest unranked; an unstamped/develop draft does not."""
        from unittest.mock import AsyncMock, MagicMock

        _patch_flag(monkeypatch, False)
        # 15 pending across both egos, unranked, >24h old.
        for i in range(15):
            created = (datetime.now(UTC) - timedelta(hours=200 - i)).isoformat()
            await ego_crud.create_proposal(
                db,
                id=f"p{i}",
                action_type="investigate",
                content=f"p{i}",
                created_at=created,
                ego_source="user_ego_cycle",
            )
        sess = MagicMock()
        sess._source_tag = "genesis_ego_cycle"
        sess._db = db
        sess._config = MagicMock()
        sess._config.max_pending_proposals = 15
        sess._proposals = MagicMock()
        sess._proposals.create_batch = AsyncMock(return_value=("b", [], []))
        sess._proposals.validate_batch = AsyncMock(return_value=[])
        sess._proposals.send_digest = AsyncMock(return_value=None)
        sess._self_development_enabled = EgoSession._self_development_enabled
        sess._process_proposals = EgoSession._process_proposals.__get__(sess)

        # One operate incoming → 15+1 > 15 → evict the global-oldest unranked.
        await sess._process_proposals(
            [{"action_type": "investigate", "content": "new", "_realist_scope": "operate"}],
            "c1",
        )
        cur = await db.execute("SELECT COUNT(*) FROM ego_proposals WHERE status='pending'")
        assert (await cur.fetchone())[0] == 14  # one evicted

    @pytest.mark.asyncio
    async def test_develop_incoming_does_not_evict(self, db, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        _patch_flag(monkeypatch, False)
        for i in range(15):
            created = (datetime.now(UTC) - timedelta(hours=200 - i)).isoformat()
            await ego_crud.create_proposal(
                db,
                id=f"q{i}",
                action_type="investigate",
                content=f"q{i}",
                created_at=created,
                ego_source="user_ego_cycle",
            )
        sess = MagicMock()
        sess._source_tag = "genesis_ego_cycle"
        sess._db = db
        sess._config = MagicMock()
        sess._config.max_pending_proposals = 15
        sess._proposals = MagicMock()
        sess._proposals.create_batch = AsyncMock(return_value=("b", [], []))
        sess._proposals.validate_batch = AsyncMock(return_value=[])
        sess._proposals.send_digest = AsyncMock(return_value=None)
        sess._self_development_enabled = EgoSession._self_development_enabled
        sess._process_proposals = EgoSession._process_proposals.__get__(sess)

        # A develop incoming will be tabled, so it must NOT evict a real pending.
        await sess._process_proposals(
            [{"action_type": "investigate", "content": "dev", "_realist_scope": "develop"}],
            "c1",
        )
        cur = await db.execute("SELECT COUNT(*) FROM ego_proposals WHERE status='pending'")
        assert (await cur.fetchone())[0] == 15  # nothing evicted

    @pytest.mark.asyncio
    async def test_develop_incoming_counts_when_enabled(self, db, monkeypatch):
        """Codex P2: when self-development is ENABLED, a develop draft stays
        pending, so it must count toward the cap and evict the oldest."""
        from unittest.mock import AsyncMock, MagicMock

        _patch_flag(monkeypatch, True)  # self-development ENABLED
        for i in range(15):
            created = (datetime.now(UTC) - timedelta(hours=200 - i)).isoformat()
            await ego_crud.create_proposal(
                db,
                id=f"r{i}",
                action_type="investigate",
                content=f"r{i}",
                created_at=created,
                ego_source="user_ego_cycle",
            )
        sess = MagicMock()
        sess._source_tag = "genesis_ego_cycle"
        sess._db = db
        sess._config = MagicMock()
        sess._config.max_pending_proposals = 15
        sess._proposals = MagicMock()
        sess._proposals.create_batch = AsyncMock(return_value=("b", [], []))
        sess._proposals.validate_batch = AsyncMock(return_value=[])
        sess._proposals.send_digest = AsyncMock(return_value=None)
        sess._self_development_enabled = EgoSession._self_development_enabled
        sess._process_proposals = EgoSession._process_proposals.__get__(sess)

        await sess._process_proposals(
            [{"action_type": "investigate", "content": "dev", "_realist_scope": "develop"}],
            "c1",
        )
        cur = await db.execute("SELECT COUNT(*) FROM ego_proposals WHERE status='pending'")
        assert (await cur.fetchone())[0] == 14  # develop counts under flag-on → evicts


class TestDeliverableAndAudit:
    def test_ensure_spec_injects_for_genesis_dispatch(self):
        s = ensure_deliverable_spec(
            {},
            proposal_id="p1",
            action_type="investigate",
            ego_source="genesis_ego_cycle",
        )
        assert parse_expected_outputs(s).files == ["~/.genesis/output/ego-reports/p1.md"]

    def test_ensure_spec_user_ego_untouched(self):
        assert (
            ensure_deliverable_spec(
                None,
                proposal_id="p1",
                action_type="dispatch",
                ego_source="user_ego_cycle",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_revise_snapshots_prior_scope(self, db, monkeypatch):
        _patch_flag(monkeypatch, False)
        await _pending(db, id="rv4", content="v1", scope="operate")
        await _revise_sess(db)._reconcile_revise(
            "rv4",
            {"id": "rv4", "revision_num": 1, "urgency": "normal", "action_type": "investigate"},
            {"content": "v2"},
            "r",
            scope="operate",
        )
        cur = await db.execute("SELECT scope FROM ego_proposal_revisions WHERE proposal_id = 'rv4'")
        assert (await cur.fetchone())["scope"] == "operate"  # prior scope archived
