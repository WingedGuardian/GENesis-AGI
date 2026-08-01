"""Tests for the follow_up MCP tools — work_state → lane derivation, revisit
condition gate, the hot(follow_up)/cold(tabled) lanes, and the update path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.db.crud import follow_ups
from genesis.mcp.health import follow_up_tools

pytestmark = pytest.mark.asyncio


# ─── work_state → kind derivation (create) ───────────────────────────────────


@pytest.mark.parametrize(
    ("work_state", "expected_kind"),
    [
        ("ready", "follow_up"),
        ("blocked_on_trigger", "follow_up"),
        ("deferred_cold", "tabled"),
    ],
)
async def test_create_work_state_derives_kind(db, work_state, expected_kind):
    """The caller declares work_state; the tool DERIVES the lane (kind)."""
    kwargs = {"revisit_condition": "when X lands"} if work_state == "blocked_on_trigger" else {}
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="do the thing",
            reason="because",
            strategy="ego_judgment",
            work_state=work_state,
            **kwargs,
        )
    assert "id" in res, res
    assert res["kind"] == expected_kind
    assert res["work_state"] == work_state
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["kind"] == expected_kind


async def test_create_ready_has_no_revisit_condition(db):
    """A 'ready' item needs no trigger; revisit_condition stays NULL."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="just do it",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
    assert res["revisit_condition"] is None
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["revisit_condition"] is None


async def test_create_deferred_cold_is_off_actionable_surface(db):
    """deferred_cold → tabled: tracked but never on the actionable surface."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="maybe explore idea Y someday",
            reason="interesting, not now",
            strategy="ego_judgment",
            work_state="deferred_cold",
        )
    assert res["kind"] == "tabled"
    actionable = await follow_ups.get_actionable(db, limit=50)
    assert all(r["id"] != res["id"] for r in actionable)


# ─── the blocked_on_trigger revisit_condition gate ───────────────────────────


async def test_blocked_on_trigger_requires_revisit_condition(db):
    """blocked_on_trigger with no trigger → teaching error, NO row created."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="waiting on something",
            reason="r",
            strategy="ego_judgment",
            work_state="blocked_on_trigger",
        )
        recent = await follow_ups.get_recent(db, limit=50, include_tabled=True)
    assert "error" in res
    assert "revisit_condition" in res["error"]
    assert not any(r["content"] == "waiting on something" for r in recent)


async def test_blocked_on_trigger_whitespace_condition_errors(db):
    """A whitespace-only trigger is treated as empty → gate fires."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="x",
            reason="r",
            strategy="ego_judgment",
            work_state="blocked_on_trigger",
            revisit_condition="   ",
        )
    assert "error" in res
    assert "revisit_condition" in res["error"]


async def test_blocked_on_trigger_with_condition_round_trips(db):
    """A named trigger is stored (stripped) and round-trips through the row + response."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="ship after review",
            reason="r",
            strategy="ego_judgment",
            work_state="blocked_on_trigger",
            revisit_condition="  when PR #123 merges  ",
        )
    assert res["kind"] == "follow_up"
    assert res["revisit_condition"] == "when PR #123 merges"
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["revisit_condition"] == "when PR #123 merges"


async def test_deferred_cold_may_carry_optional_condition(db):
    """deferred_cold does NOT require a trigger but may store one."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="someday, maybe",
            reason="r",
            strategy="ego_judgment",
            work_state="deferred_cold",
            revisit_condition="if a user ever asks for it",
        )
    assert res["kind"] == "tabled"
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["revisit_condition"] == "if a user ever asks for it"


# ─── work_state validation / requiredness ────────────────────────────────────


async def test_create_invalid_work_state_errors(db):
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="x",
            reason="y",
            strategy="ego_judgment",
            work_state="bogus",
        )
    assert "error" in res
    assert "work_state" in res["error"]


async def test_create_work_state_is_required(db):
    """work_state has no default — omitting it is a hard TypeError, not a silent lane."""
    with patch.object(follow_up_tools, "_get_db", return_value=db), pytest.raises(TypeError):
        await follow_up_tools._impl_follow_up_create(
            content="x",
            reason="y",
            strategy="ego_judgment",
        )


# ─── update path: lane moves via work_state (kind deprecated) ─────────────────


async def test_update_relanes_via_work_state(db):
    """follow_up_update work_state='deferred_cold' demotes an item to the cold lane."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="reconsider later",
            reason="not now",
            strategy="ego_judgment",
            work_state="ready",
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(fid, work_state="deferred_cold")
    assert res.get("kind") == "tabled", res
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "tabled"


async def test_update_no_longer_accepts_raw_kind(db):
    """The deprecated raw `kind` lane override was removed from follow_up_update —
    lane moves go through work_state on BOTH surfaces (create + update), closing the
    free-string / priority-reasoning door on the update path too."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="x",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        fid = created["id"]
        with pytest.raises(TypeError):
            await follow_up_tools._impl_follow_up_update(fid, kind="tabled")


async def test_update_to_blocked_on_trigger_requires_condition(db):
    """Moving to blocked_on_trigger needs a trigger (new or already on the row)."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="x",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        fid = created["id"]
        # No condition supplied and none on the row → error.
        res_err = await follow_up_tools._impl_follow_up_update(fid, work_state="blocked_on_trigger")
        # Supplying one satisfies the gate.
        res_ok = await follow_up_tools._impl_follow_up_update(
            fid,
            work_state="blocked_on_trigger",
            revisit_condition="when Y happens",
        )
    assert "error" in res_err
    assert "revisit_condition" in res_err["error"]
    assert res_ok["kind"] == "follow_up"
    assert res_ok["revisit_condition"] == "when Y happens"
    # The gate failure left NO partial change: row is still the original follow_up.
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "follow_up"


async def test_update_invalid_work_state_errors(db):
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="z",
            reason="z",
            strategy="ego_judgment",
            work_state="ready",
        )
        res = await follow_up_tools._impl_follow_up_update(created["id"], work_state="nope")
    assert "error" in res
    assert "work_state" in res["error"]


# ─── list: tabled excluded by default ────────────────────────────────────────


async def test_list_excludes_tabled_by_default(db):
    """follow_up_list hides tabled items from the agent view and reports them separately."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="actionable thing",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        await follow_up_tools._impl_follow_up_create(
            content="someday idea",
            reason="r",
            strategy="ego_judgment",
            work_state="deferred_cold",
        )
        res = await follow_up_tools._impl_follow_up_list()

    kinds = [f["kind"] for f in res["follow_ups"]]
    assert "tabled" not in kinds
    assert res.get("tabled_count") == 1
    assert res["total"] == 1


async def test_list_include_tabled_shows_them(db):
    """Opting in surfaces tabled items alongside actionable ones."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="actionable thing",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        await follow_up_tools._impl_follow_up_create(
            content="someday idea",
            reason="r",
            strategy="ego_judgment",
            work_state="deferred_cold",
        )
        res = await follow_up_tools._impl_follow_up_list(include_tabled=True)

    kinds = sorted(f["kind"] for f in res["follow_ups"])
    assert kinds == ["follow_up", "tabled"]


async def test_list_returns_revisit_condition(db):
    """The list surfaces revisit_condition so a future session sees the trigger."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="blocked item",
            reason="r",
            strategy="ego_judgment",
            work_state="blocked_on_trigger",
            revisit_condition="after the migration lands",
        )
        res = await follow_up_tools._impl_follow_up_list()
    item = next(f for f in res["follow_ups"] if f["content"] == "blocked item")
    assert item["revisit_condition"] == "after the migration lands"


async def test_list_status_filter_excludes_tabled(db):
    """The status_filter path (get_by_status) also excludes tabled by default."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="actionable pending",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        await follow_up_tools._impl_follow_up_create(
            content="tabled pending",
            reason="r",
            strategy="ego_judgment",
            work_state="deferred_cold",
        )
        res = await follow_up_tools._impl_follow_up_list(status_filter="pending")
    kinds = [f["kind"] for f in res["follow_ups"]]
    assert kinds == ["follow_up"]

    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res_incl = await follow_up_tools._impl_follow_up_list(
            status_filter="pending",
            include_tabled=True,
        )
    assert sorted(f["kind"] for f in res_incl["follow_ups"]) == ["follow_up", "tabled"]


# ─── d67c83c7 / #1198: notes-only preserves status; blocked_reason blocks ─────
# These guard the lost-update contract and MUST stay untouched by the work_state
# work — the update path still routes notes-only writes through update_notes.


async def test_update_notes_only_preserves_status(db):
    """A notes-only follow_up_update must NOT change status (routes to update_notes)."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="active work",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        fid = created["id"]
        await follow_up_tools._impl_follow_up_update(fid, status="in_progress")
        res = await follow_up_tools._impl_follow_up_update(
            fid,
            resolution_notes="a progress note, no status",
        )
    assert res["status"] == "in_progress"  # preserved, not flipped
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "in_progress"
    assert row["resolution_notes"] == "a progress note, no status"


async def test_update_blocked_reason_sets_blocked(db):
    """blocked_reason without an explicit status sets status='blocked' (documented contract)."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="thing",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(
            fid,
            blocked_reason="waiting on upstream PR",
        )
    assert res["status"] == "blocked"
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "waiting on upstream PR"


async def test_notes_only_does_not_write_stale_status(db):
    """Lost-update regression (d67c83c7): with a STALE in_progress read but the DB
    row already completed by a concurrent writer, a notes-only update must write
    ONLY notes — never the stale status back. RED on the old elif branch (which
    re-wrote existing['status'])."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="racy",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
        fid = created["id"]
        # Concurrent writer (e.g. ego resolve_follow_ups) completes it.
        await follow_ups.update_status(db, fid, "completed", resolution_notes="ego done")

        # This call's reads all return a STALE in_progress snapshot.
        async def _stale_get(_db, _id):
            return {"id": fid, "status": "in_progress", "priority": "medium"}

        with patch.object(follow_ups, "get_by_id", _stale_get):
            await follow_up_tools._impl_follow_up_update(fid, resolution_notes="fg note")

    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "completed"  # stale in_progress NOT written back
    assert row["resolution_notes"] == "fg note"
