"""Tests for the follow_up MCP tools — work_state → lane derivation, revisit
condition gate, the hot(follow_up)/cold(tabled) lanes, and the update path.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.db.crud import follow_ups
from genesis.mcp.health import follow_up_tools

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _foreground_by_default(monkeypatch):
    """Pin source='foreground_session' by default so tests are deterministic
    regardless of the runner's env (a CC session sets GENESIS_CC_SESSION=1 on
    children, which would otherwise flip source→ego_dispatch and route to tabled).
    Tests that want the autonomous path set the env explicitly."""
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)


# ─── sacred-board source authorization (autonomous → tabled) ─────────────────


async def test_autonomous_dispatch_routed_to_cold_lane(db, monkeypatch):
    """An autonomous/dispatched CC session's LLM-authored follow-up is forced to
    the COLD tabled lane even when its work_state would be 'ready' — the hot board
    is reserved for sanctioned (foreground) paths. Root-cause fix for 030e6ddc."""
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="an autonomously-suggested task the dispatched session invented",
            reason="autonomous session proposed it",
            strategy="ego_judgment",
            work_state="ready",  # would be hot 'follow_up' from a foreground session
        )
    assert res["kind"] == "tabled", res
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["kind"] == "tabled"
    assert row["source"] == "ego_dispatch"
    # not on the actionable board
    actionable = await follow_ups.get_actionable(db, limit=50)
    assert all(r["id"] != res["id"] for r in actionable)
    assert "cold" in res["message"].lower() or "tabled" in res["message"].lower()


async def test_autonomous_cannot_promote_off_tabled_via_update(db, monkeypatch):
    """The sacred-board guarantee must hold at UPDATE time too: an autonomous
    session cannot flip a tabled item back onto the hot board via follow_up_update
    (the create→tabled→update(ready) bypass). Foreground promotion still works."""
    # Foreground creates a tabled item.
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="cold item",
            reason="r",
            strategy="ego_judgment",
            work_state="deferred_cold",
        )
    fid = created["id"]
    assert created["kind"] == "tabled"

    # Autonomous session tries to promote it to the board → blocked, stays tabled.
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_update(
            follow_up_id=fid,
            work_state="ready",
        )
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "tabled", (res, row["kind"])

    # A foreground session CAN promote it.
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_update(follow_up_id=fid, work_state="ready")
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "follow_up"


async def test_autonomous_reaffirm_of_board_item_does_not_demote(db, monkeypatch):
    """The update gate fires only on a genuine off-board→board PROMOTION. An autonomous
    session re-affirming an already-on-board item (work_state='ready' on an existing
    kind='follow_up', e.g. bumping priority) must NOT silently demote it to tabled."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="board item",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
        )
    fid = created["id"]
    assert created["kind"] == "follow_up"
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_update(follow_up_id=fid, work_state="ready")
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "follow_up", "autonomous re-affirm must not demote a board item"


async def test_foreground_ready_stays_on_the_board(db):
    """A foreground session's 'ready' follow-up lands on the hot board (unchanged)."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="fix the thing the user asked for",
            reason="user asked",
            strategy="ego_judgment",
            work_state="ready",
        )
    assert res["kind"] == "follow_up", res
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["kind"] == "follow_up"
    assert row["source"] == "foreground_session"


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


# ─── Codex P1/P2: blocked_on_trigger dispatch semantics + ready clears trigger ─


async def test_create_blocked_on_trigger_rejects_surplus_task(db):
    """surplus_task dispatches immediately (dispatcher.py:63-70); pairing it with
    blocked_on_trigger (wait for an event) would run before the trigger → rejected."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="analyze after X",
            reason="r",
            strategy="surplus_task",
            work_state="blocked_on_trigger",
            revisit_condition="after event X",
        )
        recent = await follow_ups.get_recent(db, limit=50, include_tabled=True)
    assert "error" in res
    assert "surplus_task" in res["error"]
    assert not any(r["content"] == "analyze after X" for r in recent)


async def test_create_blocked_on_trigger_allows_scheduled_task(db):
    """scheduled_task IS a valid trigger — the scheduler fires it at scheduled_at."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="run at time T",
            reason="r",
            strategy="scheduled_task",
            work_state="blocked_on_trigger",
            revisit_condition="at 2026-08-01T00:00:00",
            scheduled_at="2026-08-01T00:00:00",
        )
    assert res.get("kind") == "follow_up", res
    assert res["revisit_condition"] == "at 2026-08-01T00:00:00"


async def test_create_ready_ignores_supplied_revisit_condition(db):
    """A 'ready' item has no trigger — any supplied revisit_condition is dropped."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_create(
            content="just do it",
            reason="r",
            strategy="ego_judgment",
            work_state="ready",
            revisit_condition="spurious",
        )
    assert res["revisit_condition"] is None
    row = await follow_ups.get_by_id(db, res["id"])
    assert row["revisit_condition"] is None


async def test_update_to_ready_clears_revisit_condition(db):
    """Moving a blocked_on_trigger item to ready clears the now-obsolete trigger."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="was blocked",
            reason="r",
            strategy="ego_judgment",
            work_state="blocked_on_trigger",
            revisit_condition="when Y",
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(fid, work_state="ready")
    assert res["revisit_condition"] is None, res
    row = await follow_ups.get_by_id(db, fid)
    assert row["revisit_condition"] is None


async def test_update_to_blocked_on_trigger_rejects_surplus_task_row(db):
    """A surplus_task row can't move to blocked_on_trigger (same dispatch conflict);
    the gate fires before any write, so the row is unchanged."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="surplus item",
            reason="r",
            strategy="surplus_task",
            work_state="ready",
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(
            fid,
            work_state="blocked_on_trigger",
            revisit_condition="after Z",
        )
    assert "error" in res
    assert "surplus_task" in res["error"]
    row = await follow_ups.get_by_id(db, fid)
    assert row["kind"] == "follow_up"
    assert row["revisit_condition"] is None


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


# ─── H4a/H4c: short-prefix id resolution + loud failed lookups ────────────────
# Root cause of the July graph-bake-off loss: a session passed an 8-char id
# prefix to follow_up_update; exact-match get_by_id returned a soft "not found"
# dict the session skimmed past, so a hard-dated commitment silently died.


async def test_update_accepts_unique_8char_prefix(db):
    """THE incident repro: an 8-char hex prefix must resolve to the row and the
    update must land. RED today (exact-match get_by_id → not-found dict)."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="commit the bake-off",
            reason="r",
            strategy="user_input_needed",
            work_state="ready",
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(
            fid[:8], priority="high", resolution_notes="revived"
        )
    assert "error" not in res, res
    assert res["id"] == fid
    assert res.get("resolved_from") == fid[:8]
    row = await follow_ups.get_by_id(db, fid)
    assert row["priority"] == "high"
    assert row["resolution_notes"] == "revived"


async def test_update_not_found_is_loud(db):
    """A truly-unknown id must return error_code='not_found' and a message that
    states the NON-EFFECT explicitly. RED today (bare string, no error_code)."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        res = await follow_up_tools._impl_follow_up_update(
            "ffffffffffffffffffffffffffffffff", status="completed"
        )
    assert res.get("error_code") == "not_found", res
    assert "did NOT" in res["error"] or "NOTHING" in res["error"]


async def test_update_ambiguous_prefix_rejected_no_write(db):
    """An ambiguous prefix must be rejected (error_code='ambiguous_id') and change
    NOTHING — never guess a row."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        a = await follow_up_tools._impl_follow_up_create(
            content="row A", reason="r", strategy="ego_judgment", work_state="ready"
        )
        b = await follow_up_tools._impl_follow_up_create(
            content="row B", reason="r", strategy="ego_judgment", work_state="ready"
        )
        # Force a shared 8-char prefix.
        await db.execute(
            "UPDATE follow_ups SET id = ? WHERE id = ?", ("dead0001" + "a" * 24, a["id"])
        )
        await db.execute(
            "UPDATE follow_ups SET id = ? WHERE id = ?", ("dead0001" + "b" * 24, b["id"])
        )
        await db.commit()
        res = await follow_up_tools._impl_follow_up_update("dead0001", priority="critical")
    assert res.get("error_code") == "ambiguous_id", res
    row_a = await follow_ups.get_by_id(db, "dead0001" + "a" * 24)
    assert row_a["priority"] != "critical"  # no write happened


async def test_update_full_length_unknown_id_not_prefix_guessed(db):
    """A full-length (32-char) unknown id must NOT be prefix-matched to a
    coincidental longer/shorter row — it passes through to a clean not-found."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await follow_up_tools._impl_follow_up_create(
            content="real", reason="r", strategy="ego_judgment", work_state="ready"
        )
        res = await follow_up_tools._impl_follow_up_update("0" * 32, status="completed")
    assert res.get("error_code") == "not_found", res


async def test_update_accepts_id_tagged_full_handle(db):
    """A full-length id handed in as an ``id:<32hex>`` handle — the exact shape the
    proactive hook / memory_expand emit — must resolve and update end-to-end. RED
    before the passthrough-normalization fix: the ``id:``-tagged string reached
    exact-match get_by_id un-normalized → not_found, silently dropping the update."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="tagged-handle row",
            reason="r",
            strategy="user_input_needed",
            work_state="ready",
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(
            "id:" + fid, status="completed", resolution_notes="closed via tagged id"
        )
    assert "error" not in res, res
    assert res["id"] == fid
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "completed"


# ─── H2: reject the orphan-making status='scheduled' transition ───────────────
# status='scheduled' is set ONLY by link_task() atomically with linked_task_id.
# follow_up_update can set the status but not the link, so a manual
# follow_up_update(status='scheduled') orphans the row invisibly (not in
# get_actionable / get_scheduled_due / get_linked_active). This is what July 10
# attempted. blocked is visible and intentionally NOT gated.


async def test_update_scheduled_without_link_rejected(db):
    """follow_up_update(status='scheduled') on an unlinked row is rejected with
    error_code='scheduled_needs_link' and changes nothing. RED today (succeeds,
    orphans the row)."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="revisit later", reason="r", strategy="user_input_needed", work_state="ready"
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(fid, status="scheduled")
    assert res.get("error_code") == "scheduled_needs_link", res
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "pending"  # unchanged — no orphan created


async def test_update_scheduled_with_existing_link_allowed(db):
    """A row that already carries a linked_task_id CAN be set scheduled (it stays
    visible via get_linked_active) — the gate only blocks the orphan case."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="linked", reason="r", strategy="surplus_task", work_state="ready"
        )
        fid = created["id"]
        await follow_ups.link_task(db, fid, "task-123")  # sets linked_task_id + scheduled
        # Move it off scheduled, then re-affirm scheduled via the tool.
        await follow_ups.update_status(db, fid, "in_progress")
        res = await follow_up_tools._impl_follow_up_update(fid, status="scheduled")
    assert "error" not in res, res
    row = await follow_ups.get_by_id(db, fid)
    assert row["status"] == "scheduled"


async def test_update_blocked_still_works_untouched(db):
    """Regression guard: 'blocked' is visible in get_actionable and is NOT gated —
    the bare-blocked_reason contract stays intact after H2."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        created = await follow_up_tools._impl_follow_up_create(
            content="thing", reason="r", strategy="ego_judgment", work_state="ready"
        )
        fid = created["id"]
        res = await follow_up_tools._impl_follow_up_update(fid, blocked_reason="waiting on PR")
    assert res["status"] == "blocked"
    actionable = await follow_ups.get_actionable(db, limit=50)
    assert any(r["id"] == fid for r in actionable)  # still visible


# ─── external state: the surface follow_up proposals never had ───────────────
#
# A follow-up's issue link and its repo-pulse completion proposal both live
# outside the follow_ups row. Neither was reachable from the listing, so a triage
# pass could not tell a filed row from an unfiled one, and completion proposals
# for follow_up targets were written and then rendered nowhere at all (the
# charter block is ledger-only by design — it renders a session_ledger_update
# confirm command — and the dashboard panel lists ledger only).


async def _seed_posted_issue(db, *, follow_up_id, number=101, repo="Owner/Repo"):
    await db.execute(
        "INSERT INTO pending_issue_posts "
        "(id, request_id, repo, title, body, source, source_ref, cell_domain, cell_verb,"
        " cell_risk_class, held_at, mode, status, issue_number, issue_url, adopted) "
        "VALUES (?, ?, ?, 't', 'b', 'follow_up', ?, 'github', 'issue_create', 'bulk',"
        " '2026-01-01T00:00:00', 'live', 'posted', ?, ?, 0)",
        (f"p-{number}", f"req-{number}", repo, follow_up_id, number, f"https://x/{number}"),
    )
    await db.commit()


async def _seed_proposal(db, *, item_id, pr_number=42):
    await db.execute(
        "INSERT INTO repo_pulse_annotations "
        "(id, run_id, observed_at, tier, item_id, item_text, pr_number, pr_title,"
        " confidence, rationale, status, target_kind) "
        "VALUES (?, 'r1', '2026-01-02T00:00:00', 'exact', ?, 'txt', ?, 'a pr title',"
        " 0.9, 'why', 'proposed', 'follow_up')",
        (f"ann-{item_id}-{pr_number}", item_id, pr_number),
    )
    await db.commit()


async def _one_followup(db, content="a row that triage will look at"):
    res = await follow_up_tools._impl_follow_up_create(
        content=content, reason="r", strategy="ego_judgment", work_state="ready"
    )
    return res["id"]


async def test_list_surfaces_the_linked_issue(db):
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db)
        await _seed_posted_issue(db, follow_up_id=fid, number=777)
        res = await follow_up_tools._impl_follow_up_list()
    row = next(r for r in res["follow_ups"] if r["id"] == fid)
    assert row["issue"]["number"] == 777
    assert row["issue"]["url"] == "https://x/777"
    assert res["external_state"]["issues"] == "ok"


async def test_list_surfaces_a_pulse_completion_proposal(db):
    """THE REPRO for the missing surface: this annotation is written for a
    follow_up target, and before this change no caller rendered it anywhere."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db)
        await _seed_proposal(db, item_id=fid, pr_number=1313)
        res = await follow_up_tools._impl_follow_up_list()
    row = next(r for r in res["follow_ups"] if r["id"] == fid)
    assert row["pulse_proposal"]["pr_number"] == 1313
    assert row["pulse_proposal"]["pr_title"] == "a pr title"
    assert res["external_state"]["proposals"] == "ok"


async def test_unlinked_row_has_no_issue_key_at_all(db):
    """Absence, not None — a present-but-null key reads as 'checked, has none',
    which is a different claim from 'no link found'."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db)
        res = await follow_up_tools._impl_follow_up_list()
    row = next(r for r in res["follow_ups"] if r["id"] == fid)
    assert "issue" not in row
    assert "pulse_proposal" not in row


async def test_proposal_for_a_different_row_is_not_attached(db):
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db, content="the row under test, left untouched")
        await _seed_proposal(db, item_id="some-other-id-entirely", pr_number=9)
        res = await follow_up_tools._impl_follow_up_list()
    row = next(r for r in res["follow_ups"] if r["id"] == fid)
    assert "pulse_proposal" not in row


async def test_absent_pulse_tables_report_unavailable_not_clean(db):
    """Fail-closed: 'could not look' must not be reported as 'looked, found
    nothing'. A pre-0062 install has no pulse tables; the listing must still
    return, and must say the proposal source was unreadable rather than implying
    the row has no pending proposal."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db)
        await db.execute("DROP TABLE IF EXISTS repo_pulse_annotations")
        await db.commit()
        res = await follow_up_tools._impl_follow_up_list()
    assert "error" not in res, "a missing optional table must not fail the listing"
    assert res["external_state"]["proposals"] == "unavailable"
    # the independent source still worked
    assert res["external_state"]["issues"] == "ok"
    assert any(r["id"] == fid for r in res["follow_ups"])


async def test_absent_issue_table_reports_unavailable_not_clean(db):
    """MIRROR of the pulse case, for the OTHER source. Without this the issues
    half of the fail-closed mechanism is untested: a mutation making availability
    unconditionally 'ok' changes behaviour only when this branch raises, and
    nothing else in this file makes it raise."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db)
        await db.execute("DROP TABLE IF EXISTS pending_issue_posts")
        await db.commit()
        res = await follow_up_tools._impl_follow_up_list()
    assert "error" not in res, "a missing optional table must not fail the listing"
    assert res["external_state"]["issues"] == "unavailable"
    assert res["external_state"]["proposals"] == "ok", "the sources are independent"
    assert any(r["id"] == fid for r in res["follow_ups"])


async def test_newest_proposal_wins_when_a_row_has_several(db):
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db)
        await _seed_proposal(db, item_id=fid, pr_number=100)
        await db.execute(
            "UPDATE repo_pulse_annotations SET observed_at = '2026-01-01T00:00:00'"
            " WHERE pr_number = 100"
        )
        await _seed_proposal(db, item_id=fid, pr_number=200)
        await db.execute(
            "UPDATE repo_pulse_annotations SET observed_at = '2026-06-01T00:00:00'"
            " WHERE pr_number = 200"
        )
        await db.commit()
        res = await follow_up_tools._impl_follow_up_list()
    row = next(r for r in res["follow_ups"] if r["id"] == fid)
    assert row["pulse_proposal"]["pr_number"] == 200, "the most recent proposal must win"


async def test_proposal_lookup_cannot_be_truncated_by_a_row_cap(db):
    """The query is bounded by the rows being decorated, not by a limit. Seed far
    more proposals than the listing returns and assert the decorated row still
    resolves — a global scan under a default cap would drop the oldest and report
    'ok', which is a truncated read wearing a complete one's clothes."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        fid = await _one_followup(db)
        # Its proposal is the OLDEST, so a DESC-ordered capped scan cuts it.
        await _seed_proposal(db, item_id=fid, pr_number=1)
        await db.execute(
            "UPDATE repo_pulse_annotations SET observed_at = '2020-01-01T00:00:00'"
            " WHERE pr_number = 1"
        )
        # MUST exceed list_annotations' default cap (500) or this test asserts
        # nothing: under the cap a global scan still finds the row and the test
        # passes with the bounding deleted. Verified by mutation — at 39 rows it
        # did exactly that.
        await db.executemany(
            "INSERT INTO repo_pulse_annotations "
            "(id, run_id, observed_at, tier, item_id, item_text, pr_number, pr_title,"
            " confidence, rationale, status, target_kind) "
            "VALUES (?, 'r1', '2026-06-01T00:00:00', 'exact', ?, 'txt', ?, 't',"
            " 0.9, 'why', 'proposed', 'follow_up')",
            [(f"ann-noise-{n}", f"noise-{n}", n) for n in range(2, 620)],
        )
        await db.commit()
        res = await follow_up_tools._impl_follow_up_list(limit=5)
    row = next(r for r in res["follow_ups"] if r["id"] == fid)
    assert row["pulse_proposal"]["pr_number"] == 1
    assert res["external_state"]["proposals"] == "ok"


async def test_empty_listing_does_not_fabricate_availability(db):
    """With no rows to decorate the sources are still probed, so the map reports
    what was actually reachable rather than asserting 'ok' for a check that never
    ran."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        await db.execute("DROP TABLE IF EXISTS pending_issue_posts")
        await db.commit()
        res = await follow_up_tools._impl_follow_up_list(status_filter="failed")
    assert res["follow_ups"] == []
    assert res["external_state"]["issues"] == "unavailable"


async def test_a_busy_row_cannot_hide_another_rows_proposal(db):
    """CodeRabbit's case, and the one the previous fix missed: filtering a CAPPED
    listing by item id bounds WHICH rows are considered, not HOW MANY return. With
    one follow-up holding far more than the cap in newer proposals, a second
    follow-up's older proposal fell past the limit and its row came back
    undecorated — while external_state still reported the source as read."""
    with patch.object(follow_up_tools, "_get_db", return_value=db):
        busy = await _one_followup(db, content="the noisy row that crowds the query out")
        quiet = await _one_followup(db, content="the row whose older proposal must survive")
        # quiet's proposal is the OLDEST of all.
        await _seed_proposal(db, item_id=quiet, pr_number=1)
        await db.execute(
            "UPDATE repo_pulse_annotations SET observed_at = '2020-01-01T00:00:00'"
            " WHERE pr_number = 1"
        )
        # busy alone carries more than the 500 cap, all newer.
        await db.executemany(
            "INSERT INTO repo_pulse_annotations "
            "(id, run_id, observed_at, tier, item_id, item_text, pr_number, pr_title,"
            " confidence, rationale, status, target_kind) "
            "VALUES (?, 'r1', '2026-06-01T00:00:00', 'exact', ?, 'txt', ?, 't',"
            " 0.9, 'why', 'proposed', 'follow_up')",
            [(f"ann-busy-{n}", busy, n) for n in range(2, 620)],
        )
        await db.commit()
        res = await follow_up_tools._impl_follow_up_list(limit=50)
    rows = {r["id"]: r for r in res["follow_ups"]}
    assert rows[quiet].get("pulse_proposal", {}).get("pr_number") == 1, (
        "a busy sibling must not push this row's proposal out of the result"
    )
    assert "pulse_proposal" in rows[busy]
    assert res["external_state"]["proposals"] == "ok"
