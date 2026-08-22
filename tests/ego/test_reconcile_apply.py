"""PR-6b — reconcile live-apply tests.

Covers the parser robustness fix and the live-apply branch of the reconcile
stage: verdict application, per-draft fail-open, 12-char target-prefix
resolution, the 24h withdraw guard, and revise hash-dedup / race handling.

The apply methods only touch ``self._db`` and ``self._source_tag``, so we build
a lightweight EgoSession via ``__new__`` rather than the full constructor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.cc.types import CCOutput
from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego.integrity import content_hash
from genesis.ego.session import EgoSession, _parse_reconcile_response


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for table in ("ego_proposals", "ego_proposal_revisions", "ego_state"):
            await conn.execute(TABLES[table])
        yield conn


@pytest.fixture
def sess(db):
    """Lightweight EgoSession — only _db/_source_tag are needed by the apply
    methods; skip the heavy constructor."""
    s = EgoSession.__new__(EgoSession)
    s._db = db
    s._source_tag = "genesis_ego_cycle"
    s._last_reconcile_cost_usd = 0.0
    return s


async def _board_item(
    db,
    *,
    id: str,
    content: str = "board work",
    age_hours: float = 48.0,
    batch: str = "batch_default",
    snapshotted: bool = True,
) -> dict:
    """Insert a pending board proposal aged `age_hours` and return its row dict.

    By default writes a revision_snapshot for its batch (so a live revise of it
    is TOCTOU-safe). Pass ``snapshotted=False`` to simulate a pre-#1257 digest
    with no snapshot (revise must then reaffirm-fallback)."""
    created = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    await ego_crud.create_proposal(
        db,
        id=id,
        action_type="investigate",
        content=content,
        status="pending",
        batch_id=batch,
        created_at=created,
        content_hash=content_hash(content),
        content_size=len(content.encode()),
        ego_source="genesis_ego_cycle",
    )
    if snapshotted:
        await ego_crud.set_state(db, key=f"revision_snapshot:{batch}", value=json.dumps({id: 1}))
    row = await ego_crud.get_proposal(db, id)
    return dict(row)


def _draft(content: str = "fresh draft") -> dict:
    return {"action_type": "investigate", "content": content, "confidence": 0.8}


# ---------------------------------------------------------------------------
# Parser robustness (the ~13% preamble bug)
# ---------------------------------------------------------------------------


class TestParserRobustness:
    def test_preamble_prose_with_brackets_parsed(self):
        """Model preambles with 'The draft [0] ...' then emits the array. The
        naive find('[') slice grabbed the prose bracket → {} → all 'new'. The
        regex tier must recover the real verdicts."""
        raw = (
            "I'll analyze the single draft against the board.\n\n"
            "The draft [0] proposes dispatching a weekly install test.\n\n"
            '[{"index": 0, "verdict": "reaffirm", "target_id": "a80588ac034e", '
            '"reason": "board already covers it"}]'
        )
        verdicts = _parse_reconcile_response(raw, 1)
        assert verdicts[0]["verdict"] == "reaffirm"
        assert verdicts[0]["target_id"] == "a80588ac034e"

    def test_clean_array_still_parses(self):
        raw = '[{"index": 0, "verdict": "new", "target_id": null, "reason": "x"}]'
        assert _parse_reconcile_response(raw, 1)[0]["verdict"] == "new"

    def test_fenced_json_still_parses(self):
        raw = '```json\n[{"index": 0, "verdict": "withdraw", "target_id": "abc"}]\n```'
        assert _parse_reconcile_response(raw, 1)[0]["verdict"] == "withdraw"

    def test_garbage_returns_empty(self):
        assert _parse_reconcile_response("no json here at all", 1) == {}


# ---------------------------------------------------------------------------
# Live apply — verdict handling
# ---------------------------------------------------------------------------


class TestApplyVerdicts:
    async def test_new_kept(self, sess, db):
        drafts = [_draft("novel work")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts, {0: {"verdict": "new", "target_id": None}}, []
        )
        assert survivors == drafts

    async def test_reaffirm_drops_draft_and_stamps_validated(self, sess, db):
        board_row = await _board_item(db, id="b1aaaaaaaaaa1111")
        assert board_row["last_validated_at"] is None
        drafts = [_draft("re-derived board work")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "reaffirm", "target_id": "b1aaaaaaaaaa"}},
            [board_row],
        )
        assert survivors == []  # dropped
        row = await ego_crud.get_proposal(db, "b1aaaaaaaaaa1111")
        assert row["last_validated_at"] is not None
        assert row["status"] == "pending"

    async def test_withdraw_null_target_drops_draft(self, sess, db):
        """Covered-work invalidated a FRESH draft with no board twin → drop it,
        no board mutation."""
        board_row = await _board_item(db, id="keepkeepkeep0001")
        drafts = [_draft("covered by a cron job")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts, {0: {"verdict": "withdraw", "target_id": None}}, [board_row]
        )
        assert survivors == []  # dropped
        # Unrelated board item untouched.
        assert (await ego_crud.get_proposal(db, "keepkeepkeep0001"))["status"] == "pending"

    async def test_withdraw_valid_target_retires_and_drops(self, sess, db):
        board_row = await _board_item(db, id="wd11wd11wd110000", age_hours=48)
        drafts = [_draft("dup of an invalidated item")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "withdraw", "target_id": "wd11wd11wd11", "reason": "job does it"}},
            [board_row],
        )
        assert survivors == []
        row = await ego_crud.get_proposal(db, "wd11wd11wd110000")
        assert row["status"] == "withdrawn"
        assert "job does it" in (row["user_response"] or "")

    async def test_withdraw_24h_guard_defers_board_but_drops_draft(self, sess, db):
        """A <24h board item is NOT withdrawn (user-protection guard), but the
        duplicate draft is still dropped."""
        board_row = await _board_item(db, id="young1young10000", age_hours=2)
        drafts = [_draft("dup of a fresh board item")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "withdraw", "target_id": "young1young1"}},
            [board_row],
        )
        assert survivors == []  # draft still dropped
        row = await ego_crud.get_proposal(db, "young1young10000")
        assert row["status"] == "pending"  # board NOT withdrawn (<24h)

    async def test_revise_applies_and_drops(self, sess, db):
        board_row = await _board_item(db, id="rv11rv11rv110000", content="stale text")
        drafts = [_draft("sharper, better text")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {
                0: {
                    "verdict": "revise",
                    "target_id": "rv11rv11rv11",
                    "reason": "sharper",
                    "scope": "operate",
                }
            },
            [board_row],
        )
        assert survivors == []  # dropped
        row = await ego_crud.get_proposal(db, "rv11rv11rv110000")
        assert row["content"] == "sharper, better text"
        assert row["revision_num"] == 2
        assert row["status"] == "pending"
        # Prior values archived.
        cur = await db.execute(
            "SELECT content FROM ego_proposal_revisions WHERE proposal_id = ?",
            ("rv11rv11rv110000",),
        )
        assert (await cur.fetchone())["content"] == "stale text"

    async def test_revise_unsnapshotted_reaffirms_instead(self, sess, db):
        """Codex P2: a revise verdict on a board item whose digest has NO revision
        snapshot (e.g. a pre-#1257 digest) must NOT revise — that would reopen the
        approve-time TOCTOU on a late reply (resolve falls back to unguarded). It
        reaffirms instead: board item kept as-is, duplicate draft dropped, content
        unchanged. No duplicate is created."""
        board_row = await _board_item(
            db,
            id="nosnap1nosnap100",
            content="original",
            batch="pre1257batch",
            snapshotted=False,
        )
        assert board_row["last_validated_at"] is None
        drafts = [_draft("sharper content the model wanted to apply")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "revise", "target_id": "nosnap1nosna"}},
            [board_row],
        )
        assert survivors == []  # duplicate draft dropped, NOT kept-as-new
        row = await ego_crud.get_proposal(db, "nosnap1nosnap100")
        assert row["content"] == "original"  # NOT revised (no snapshot)
        assert row["revision_num"] == 1
        assert row["last_validated_at"] is not None  # reaffirmed instead


# ---------------------------------------------------------------------------
# Fail-safe: hallucinated / ambiguous targets, races, dedup
# ---------------------------------------------------------------------------


class TestFailSafe:
    async def test_hallucinated_target_keeps_as_new(self, sess, db):
        """A target prefix that matches no board id → downgrade to new (create),
        never mutate a wrong proposal."""
        board_row = await _board_item(db, id="real1real1real00")
        drafts = [_draft("some work")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "reaffirm", "target_id": "ffffffffffff"}},  # no match
            [board_row],
        )
        assert survivors == drafts  # kept as new
        # Board item NOT reaffirmed.
        assert (await ego_crud.get_proposal(db, "real1real1real00"))["last_validated_at"] is None

    async def test_prefix_resolves_to_full_id(self, sess, db):
        """The model sees only id[:12]; the full id must be resolved from it."""
        board_row = await _board_item(db, id="abcdefabcdefXXXX")
        drafts = [_draft()]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "reaffirm", "target_id": "abcdefabcdef"}},  # 12-char prefix
            [board_row],
        )
        assert survivors == []
        assert (await ego_crud.get_proposal(db, "abcdefabcdefXXXX"))[
            "last_validated_at"
        ] is not None

    async def test_ambiguous_prefix_keeps_as_new(self, sess, db):
        """Two board ids sharing a 12-char prefix → ambiguous → fail-safe new."""
        r1 = await _board_item(db, id="dupdupdupdup0001")
        r2 = await _board_item(db, id="dupdupdupdup0002", content="other")
        drafts = [_draft()]
        survivors = await sess._apply_reconcile_verdicts(
            drafts, {0: {"verdict": "reaffirm", "target_id": "dupdupdupdup"}}, [r1, r2]
        )
        assert survivors == drafts  # kept
        assert (await ego_crud.get_proposal(db, "dupdupdupdup0001"))["last_validated_at"] is None

    async def test_revise_hash_dup_keeps_as_new(self, sess, db):
        """Revising to content that already exists on another pending item is
        skipped (revise_proposal does not dedup) → keep the draft as new."""
        board_row = await _board_item(db, id="tgt1tgt1tgt10000", content="original")
        # Another pending item already holds the target content.
        await _board_item(db, id="other1other10000", content="already pending text")
        drafts = [_draft("already pending text")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "revise", "target_id": "tgt1tgt1tgt1", "scope": "operate"}},
            [board_row],
        )
        assert survivors == drafts  # kept as new; revise skipped
        row = await ego_crud.get_proposal(db, "tgt1tgt1tgt10000")
        assert row["content"] == "original"  # unchanged
        assert row["revision_num"] == 1

    async def test_revise_nonpending_keeps_as_new(self, sess, db):
        """A board item concurrently approved → revise guard rowcount 0 → keep."""
        board_row = await _board_item(db, id="appr1appr1appr00", content="orig")
        await db.execute(
            "UPDATE ego_proposals SET status = 'approved' WHERE id = ?",
            ("appr1appr1appr00",),
        )
        await db.commit()
        drafts = [_draft("new content")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "revise", "target_id": "appr1appr1appr", "scope": "operate"}},
            [board_row],
        )
        assert survivors == drafts  # kept
        row = await ego_crud.get_proposal(db, "appr1appr1appr00")
        assert row["content"] == "orig"  # unchanged

    async def test_withdraw_unresolvable_target_keeps_as_new(self, sess, db):
        """A withdraw verdict with a NON-null but unresolvable target must be
        kept-as-new (created), NOT silently dropped — dropping would lose real
        work whose board twin couldn't be resolved. (The unresolved-target check
        precedes verdict dispatch, so this holds for every verdict, but withdraw
        is the dangerous one: its resolved path DROPS the draft.)"""
        board_row = await _board_item(db, id="exists1exists100")
        drafts = [_draft("work that must not vanish")]
        survivors = await sess._apply_reconcile_verdicts(
            drafts,
            {0: {"verdict": "withdraw", "target_id": "nomatch0nomat"}},  # non-null, no match
            [board_row],
        )
        assert survivors == drafts  # kept as new, NOT dropped
        # The real board item is untouched (no wrong withdrawal).
        assert (await ego_crud.get_proposal(db, "exists1exists100"))["status"] == "pending"

    async def test_per_draft_withdraw_failure_isolated(self, sess, db, monkeypatch):
        """A raising withdraw path fails open THAT draft (kept as new) without
        abandoning other drafts — symmetry with the reaffirm fail-open."""
        b1 = await _board_item(db, id="wok1wok1wok10000")
        b2 = await _board_item(db, id="wbad1wbad1wbad00")

        async def flaky_withdraw(dbc, pid, **kw):
            if pid == "wbad1wbad1wbad00":
                raise RuntimeError("boom")
            return True

        monkeypatch.setattr(ego_crud, "withdraw_proposal", flaky_withdraw)
        drafts = [_draft("d0"), _draft("d1")]
        verdicts = {
            0: {"verdict": "withdraw", "target_id": "wok1wok1wok1"},
            1: {"verdict": "withdraw", "target_id": "wbad1wbad1wba"},
        }
        survivors = await sess._apply_reconcile_verdicts(drafts, verdicts, [b1, b2])
        assert survivors == [drafts[1]]  # d1 failed → kept; d0 dropped

    async def test_missing_verdict_defaults_new(self, sess, db):
        drafts = [_draft("a"), _draft("b")]
        survivors = await sess._apply_reconcile_verdicts(drafts, {}, [])
        assert survivors == drafts  # both kept

    async def test_per_draft_failure_isolated(self, sess, db, monkeypatch):
        """One draft's apply raising must not abandon others or already-applied
        board decisions — the failing draft is kept as new, the rest proceed."""
        b1 = await _board_item(db, id="ok11ok11ok110000")
        b2 = await _board_item(db, id="boom1boom1boom00")

        real_reaffirm = ego_crud.reaffirm_proposal

        async def flaky_reaffirm(dbc, pid, **kwargs):
            if pid == "boom1boom1boom00":
                raise RuntimeError("boom")
            return await real_reaffirm(dbc, pid, **kwargs)

        monkeypatch.setattr(ego_crud, "reaffirm_proposal", flaky_reaffirm)

        drafts = [_draft("d0"), _draft("d1")]
        verdicts = {
            0: {"verdict": "reaffirm", "target_id": "ok11ok11ok11"},
            1: {"verdict": "reaffirm", "target_id": "boom1boom1boom"},
        }
        survivors = await sess._apply_reconcile_verdicts(drafts, verdicts, [b1, b2])
        # d0 applied (dropped); d1 failed → kept as new.
        assert survivors == [drafts[1]]
        assert (await ego_crud.get_proposal(db, "ok11ok11ok110000"))[
            "last_validated_at"
        ] is not None


# ---------------------------------------------------------------------------
# Mode gating via _reconcile_drafts
# ---------------------------------------------------------------------------


class TestModeGating:
    def _gate(self, sess, raw_text: str):
        out = CCOutput(
            session_id="s",
            text=raw_text,
            model_used="opus",
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
            duration_ms=100,
            exit_code=0,
            is_error=False,
            error_message=None,
        )
        sess._run_gate_cc = AsyncMock(return_value=out)
        sess._gather_covered_work = AsyncMock(
            return_value={"jobs": [], "stale_jobs": [], "merged_prs": []}
        )

    async def test_shadow_applies_nothing(self, sess, db, monkeypatch):
        import genesis.ego.reconcile_config as rc

        monkeypatch.setattr(rc, "effective_mode", lambda: "shadow")
        await _board_item(db, id="shadow1shadow100")
        self._gate(
            sess,
            '[{"index": 0, "verdict": "reaffirm", "target_id": "shadow1shadow"}]',
        )
        drafts = [_draft("re-derived")]
        out = await sess._reconcile_drafts(drafts)
        assert out == drafts  # unchanged — shadow observes only
        assert (await ego_crud.get_proposal(db, "shadow1shadow100"))["last_validated_at"] is None

    async def test_live_applies(self, sess, db, monkeypatch):
        import genesis.ego.reconcile_config as rc

        monkeypatch.setattr(rc, "effective_mode", lambda: "live")
        # board must be discoverable via list_pending_proposals(ego_source=tag)
        await _board_item(db, id="live1live1live000")
        self._gate(
            sess,
            '[{"index": 0, "verdict": "reaffirm", "target_id": "live1live1liv"}]',
        )
        drafts = [_draft("re-derived")]
        out = await sess._reconcile_drafts(drafts)
        assert out == []  # draft dropped (reaffirmed)
        assert (await ego_crud.get_proposal(db, "live1live1live000"))[
            "last_validated_at"
        ] is not None

    async def test_off_skips_entirely(self, sess, db, monkeypatch):
        import genesis.ego.reconcile_config as rc

        monkeypatch.setattr(rc, "effective_mode", lambda: "off")
        sess._run_gate_cc = AsyncMock()
        drafts = [_draft()]
        out = await sess._reconcile_drafts(drafts)
        assert out == drafts
        sess._run_gate_cc.assert_not_called()
