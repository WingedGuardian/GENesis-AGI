"""Tests for the ego intentions queue — CRUD, context, and processing."""

from __future__ import annotations

import aiosqlite
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def db(tmp_path):
    """In-memory DB with the PRODUCTION ego_intentions schema (no drift)."""
    from genesis.db.schema import TABLES

    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_intentions"])
        await conn.commit()
        yield conn


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------

class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_id(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Re-propose LinkedIn outreach",
            trigger_condition="When sprint ends",
            ego_source="user_ego_cycle",
            reasoning="Rejected May 3",
        )
        assert iid is not None
        assert len(iid) == 16

    @pytest.mark.asyncio
    async def test_create_and_list(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="Investigate Suki culture",
            trigger_condition="When application submitted",
            ego_source="user_ego_cycle",
        )
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 1
        assert items[0]["content"] == "Investigate Suki culture"
        assert items[0]["trigger_condition"] == "When application submitted"
        assert items[0]["status"] == "active"
        assert items[0]["cycle_count"] == 0

    @pytest.mark.asyncio
    async def test_cap_enforcement(self, db):
        from genesis.db.crud import ego_intentions

        for i in range(5):
            iid = await ego_intentions.create(
                db,
                content=f"Intention {i}",
                trigger_condition=f"Trigger {i}",
                ego_source="user_ego_cycle",
            )
            assert iid is not None

        # 6th should be rejected
        iid = await ego_intentions.create(
            db,
            content="Intention 5 — over cap",
            trigger_condition="Should not be stored",
            ego_source="user_ego_cycle",
        )
        assert iid is None
        assert await ego_intentions.count_active(db, "user_ego_cycle") == 5

    @pytest.mark.asyncio
    async def test_source_isolation(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="User intention",
            trigger_condition="User trigger",
            ego_source="user_ego_cycle",
        )
        await ego_intentions.create(
            db,
            content="Genesis intention",
            trigger_condition="Genesis trigger",
            ego_source="genesis_ego_cycle",
        )
        user_items = await ego_intentions.list_active(db, "user_ego_cycle")
        genesis_items = await ego_intentions.list_active(db, "genesis_ego_cycle")
        assert len(user_items) == 1
        assert len(genesis_items) == 1
        assert user_items[0]["content"] == "User intention"
        assert genesis_items[0]["content"] == "Genesis intention"

    @pytest.mark.asyncio
    async def test_cap_per_source(self, db):
        """Cap is per ego_source, not global."""
        from genesis.db.crud import ego_intentions

        for i in range(5):
            await ego_intentions.create(
                db,
                content=f"User {i}",
                trigger_condition=f"T {i}",
                ego_source="user_ego_cycle",
            )
        # Genesis ego should still have room
        iid = await ego_intentions.create(
            db,
            content="Genesis intention",
            trigger_condition="Genesis trigger",
            ego_source="genesis_ego_cycle",
        )
        assert iid is not None


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_fire(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Propose X",
            trigger_condition="When Y",
            ego_source="user_ego_cycle",
        )
        ok = await ego_intentions.fire(db, iid, proposal_id="prop_abc")
        assert ok is True

        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 0  # No longer active

        cursor = await db.execute(
            "SELECT * FROM ego_intentions WHERE id = ?", (iid,),
        )
        row = dict(await cursor.fetchone())
        assert row["status"] == "fired"
        assert row["fired_at"] is not None
        assert row["proposal_id"] == "prop_abc"

    @pytest.mark.asyncio
    async def test_double_fire_fails(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Once only",
            trigger_condition="When Z",
            ego_source="user_ego_cycle",
        )
        await ego_intentions.fire(db, iid)
        ok = await ego_intentions.fire(db, iid)
        assert ok is False

    @pytest.mark.asyncio
    async def test_withdraw(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Withdraw me",
            trigger_condition="Never",
            ego_source="user_ego_cycle",
        )
        ok = await ego_intentions.withdraw(db, iid)
        assert ok is True

        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_renew(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Renew me",
            trigger_condition="Eventually",
            ego_source="user_ego_cycle",
        )
        # Increment a few times
        for _ in range(10):
            await ego_intentions.increment_cycle_count(db, iid)

        # Renew resets to 0
        ok = await ego_intentions.renew(db, iid)
        assert ok is True

        cursor = await db.execute(
            "SELECT cycle_count FROM ego_intentions WHERE id = ?", (iid,),
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_increment_cycle_count(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Count me",
            trigger_condition="When counted",
            ego_source="user_ego_cycle",
        )
        new_count = await ego_intentions.increment_cycle_count(db, iid)
        assert new_count == 1
        new_count = await ego_intentions.increment_cycle_count(db, iid)
        assert new_count == 2

    @pytest.mark.asyncio
    async def test_expire_overdue(self, db):
        """Expiry uses strict > so the ego gets one final review at max_cycles."""
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Short lived",
            trigger_condition="Quick",
            ego_source="user_ego_cycle",
            max_cycles=2,
        )
        # At exactly max_cycles — should NOT expire yet (ego gets final review)
        await ego_intentions.increment_cycle_count(db, iid)
        await ego_intentions.increment_cycle_count(db, iid)
        expired = await ego_intentions.expire_overdue(db, "user_ego_cycle")
        assert expired == 0
        assert await ego_intentions.count_active(db, "user_ego_cycle") == 1

        # One more increment past max_cycles — NOW it expires
        await ego_intentions.increment_cycle_count(db, iid)
        expired = await ego_intentions.expire_overdue(db, "user_ego_cycle")
        assert expired == 1
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_expire_does_not_touch_other_source(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="User intention",
            trigger_condition="T",
            ego_source="user_ego_cycle",
            max_cycles=1,
        )
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        iid = items[0]["id"]
        await ego_intentions.increment_cycle_count(db, iid)

        # Expire only genesis — user should survive
        expired = await ego_intentions.expire_overdue(db, "genesis_ego_cycle")
        assert expired == 0
        assert await ego_intentions.count_active(db, "user_ego_cycle") == 1


# ---------------------------------------------------------------------------
# Context injection tests
# ---------------------------------------------------------------------------


class TestContextInjection:
    @pytest.mark.asyncio
    async def test_empty_db_shows_placeholder(self, db):
        from genesis.ego.intentions_context import build_intentions_section

        text = await build_intentions_section(db, "user_ego_cycle")
        assert "No active intentions" in text
        assert "MANDATORY REVIEW" not in text

    @pytest.mark.asyncio
    async def test_active_items_rendered(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.intentions_context import build_intentions_section

        iid = await ego_intentions.create(
            db,
            content="Propose LinkedIn outreach",
            trigger_condition="When sprint ends",
            ego_source="user_ego_cycle",
            reasoning="Rejected May 3",
            priority="high",
        )
        text = await build_intentions_section(db, "user_ego_cycle")
        assert "MANDATORY REVIEW" in text
        assert "1 active intention" in text
        assert f"id:{iid}" in text
        assert "Propose LinkedIn outreach" in text
        assert "When sprint ends" in text
        assert "Rejected May 3" in text
        assert "[high]" in text
        assert "cycle 0/20" in text

    @pytest.mark.asyncio
    async def test_remaining_slots(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.intentions_context import build_intentions_section

        for i in range(3):
            await ego_intentions.create(
                db,
                content=f"Intention {i}",
                trigger_condition=f"Trigger {i}",
                ego_source="user_ego_cycle",
            )
        text = await build_intentions_section(db, "user_ego_cycle")
        assert "2 slot(s) available" in text

    @pytest.mark.asyncio
    async def test_source_isolation_in_context(self, db):
        from genesis.db.crud import ego_intentions
        from genesis.ego.intentions_context import build_intentions_section

        await ego_intentions.create(
            db,
            content="User only",
            trigger_condition="User trigger",
            ego_source="user_ego_cycle",
        )
        text = await build_intentions_section(db, "genesis_ego_cycle")
        assert "No active intentions" in text

    @pytest.mark.asyncio
    async def test_system_rows_do_not_consume_llm_slots(self, db):
        """System follow-through rows render but don't eat the LLM's 5 slots."""
        from genesis.db.crud import ego_intentions
        from genesis.ego.intentions_context import build_intentions_section

        await ego_intentions.create(
            db,
            content="Review dispatch outcome for proposal abc12345",
            trigger_condition="Next ego cycle",
            ego_source="user_ego_cycle",
            origin="system",
            proposal_id="abc12345deadbeef",
        )
        await ego_intentions.create(
            db,
            content="LLM-created intention",
            trigger_condition="Some trigger",
            ego_source="user_ego_cycle",
        )
        text = await build_intentions_section(db, "user_ego_cycle")
        # Both rows are mandatory-review...
        assert "2 active intention" in text
        assert "Review dispatch outcome" in text
        # ...the system row is labelled as follow-through...
        assert "follow-through" in text
        # ...but only the LLM row counts against the cap (5 - 1 = 4).
        assert "4 slot(s) available" in text


# ---------------------------------------------------------------------------
# System-origin (dispatch follow-through) CRUD tests
# ---------------------------------------------------------------------------


class TestSystemOrigin:
    @staticmethod
    async def _fill_llm_cap(db, source="user_ego_cycle"):
        from genesis.db.crud import ego_intentions

        for i in range(ego_intentions.MAX_ACTIVE_PER_SOURCE):
            iid = await ego_intentions.create(
                db,
                content=f"Intention {i}",
                trigger_condition=f"Trigger {i}",
                ego_source=source,
            )
            assert iid is not None

    @pytest.mark.asyncio
    async def test_system_origin_bypasses_cap(self, db):
        """A system follow-through row is created even when the LLM cap is full."""
        from genesis.db.crud import ego_intentions

        await self._fill_llm_cap(db)
        iid = await ego_intentions.create(
            db,
            content="Review dispatch outcome for proposal abc12345",
            trigger_condition="Next ego cycle",
            ego_source="user_ego_cycle",
            origin="system",
            proposal_id="abc12345deadbeef",
        )
        assert iid is not None
        items = await ego_intentions.list_active(db, "user_ego_cycle")
        assert len(items) == 6

    @pytest.mark.asyncio
    async def test_system_rows_do_not_count_toward_llm_cap(self, db):
        """LLM creations still succeed when system rows are present."""
        from genesis.db.crud import ego_intentions

        for i in range(3):
            await ego_intentions.create(
                db,
                content=f"Follow-through {i}",
                trigger_condition="Next ego cycle",
                ego_source="user_ego_cycle",
                origin="system",
                proposal_id=f"prop{i:012d}",
            )
        # All 5 LLM slots must still be open.
        await self._fill_llm_cap(db)

    @pytest.mark.asyncio
    async def test_llm_cap_still_enforced(self, db):
        """Default-origin creation is still capped at MAX_ACTIVE_PER_SOURCE."""
        from genesis.db.crud import ego_intentions

        await self._fill_llm_cap(db)
        iid = await ego_intentions.create(
            db,
            content="One too many",
            trigger_condition="Trigger",
            ego_source="user_ego_cycle",
        )
        assert iid is None

    @pytest.mark.asyncio
    async def test_origin_and_proposal_id_persisted(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="Review dispatch outcome",
            trigger_condition="Next ego cycle",
            ego_source="genesis_ego_cycle",
            origin="system",
            proposal_id="feedfacecafebeef",
        )
        items = await ego_intentions.list_active(db, "genesis_ego_cycle")
        assert items[0]["origin"] == "system"
        assert items[0]["proposal_id"] == "feedfacecafebeef"

    @pytest.mark.asyncio
    async def test_default_origin_is_ego(self, db):
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="Plain intention",
            trigger_condition="Trigger",
            ego_source="genesis_ego_cycle",
        )
        items = await ego_intentions.list_active(db, "genesis_ego_cycle")
        assert items[0]["origin"] == "ego"

    @pytest.mark.asyncio
    async def test_has_active_for_proposal(self, db):
        from genesis.db.crud import ego_intentions

        assert not await ego_intentions.has_active_for_proposal(
            db, "user_ego_cycle", "abc12345deadbeef"
        )
        await ego_intentions.create(
            db,
            content="Review dispatch outcome",
            trigger_condition="Next ego cycle",
            ego_source="user_ego_cycle",
            origin="system",
            proposal_id="abc12345deadbeef",
        )
        assert await ego_intentions.has_active_for_proposal(
            db, "user_ego_cycle", "abc12345deadbeef"
        )
        # Different source is isolated; different proposal is distinct.
        assert not await ego_intentions.has_active_for_proposal(
            db, "genesis_ego_cycle", "abc12345deadbeef"
        )
        assert not await ego_intentions.has_active_for_proposal(
            db, "user_ego_cycle", "0000000000000000"
        )

    @pytest.mark.asyncio
    async def test_fire_without_proposal_id_preserves_creation_ref(self, db):
        """Firing with no proposal_id must not erase the system row's provenance."""
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Review dispatch outcome",
            trigger_condition="Next ego cycle",
            ego_source="user_ego_cycle",
            origin="system",
            proposal_id="abc12345deadbeef",
        )
        ok = await ego_intentions.fire(db, iid, ego_source="user_ego_cycle")
        assert ok is True
        cursor = await db.execute(
            "SELECT proposal_id FROM ego_intentions WHERE id = ?", (iid,)
        )
        row = await cursor.fetchone()
        assert row["proposal_id"] == "abc12345deadbeef"

    @pytest.mark.asyncio
    async def test_increment_unreviewed_ages_unmentioned_rows(self, db):
        """Implicit-keep: rows omitted from the review output still age."""
        from genesis.db.crud import ego_intentions

        reviewed = await ego_intentions.create(
            db,
            content="Reviewed row",
            trigger_condition="T",
            ego_source="user_ego_cycle",
        )
        unmentioned = await ego_intentions.create(
            db,
            content="Unmentioned row",
            trigger_condition="T",
            ego_source="user_ego_cycle",
        )
        other_source = await ego_intentions.create(
            db,
            content="Other ego's row",
            trigger_condition="T",
            ego_source="genesis_ego_cycle",
        )
        bumped = await ego_intentions.increment_unreviewed(
            db, "user_ego_cycle", [reviewed]
        )
        assert bumped == 1
        rows = {
            r["id"]: r["cycle_count"]
            for r in await db.execute_fetchall(
                "SELECT id, cycle_count FROM ego_intentions"
            )
        }
        assert rows[unmentioned] == 1
        assert rows[reviewed] == 0  # excluded (explicit keep bumps separately)
        assert rows[other_source] == 0  # cross-ego isolation

    @pytest.mark.asyncio
    async def test_increment_unreviewed_empty_reviewed_ages_all(self, db):
        """LLM omitted the whole review block — every active row still ages."""
        from genesis.db.crud import ego_intentions

        a = await ego_intentions.create(
            db, content="A", trigger_condition="T", ego_source="user_ego_cycle"
        )
        b = await ego_intentions.create(
            db,
            content="B",
            trigger_condition="T",
            ego_source="user_ego_cycle",
            origin="system",
            proposal_id="abc12345deadbeef",
        )
        bumped = await ego_intentions.increment_unreviewed(db, "user_ego_cycle", [])
        assert bumped == 2
        rows = {
            r["id"]: r["cycle_count"]
            for r in await db.execute_fetchall(
                "SELECT id, cycle_count FROM ego_intentions"
            )
        }
        assert rows[a] == 1 and rows[b] == 1

    @pytest.mark.asyncio
    async def test_increment_unreviewed_skips_inactive(self, db):
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db, content="A", trigger_condition="T", ego_source="user_ego_cycle"
        )
        await ego_intentions.withdraw(db, iid, ego_source="user_ego_cycle")
        bumped = await ego_intentions.increment_unreviewed(db, "user_ego_cycle", [])
        assert bumped == 0

    @pytest.mark.asyncio
    async def test_mechanical_ttl_system_row_expires_without_compliance(self, db):
        """A follow-through row the ego NEVER reviews is reaped at max_cycles=3."""
        from genesis.db.crud import ego_intentions

        await ego_intentions.create(
            db,
            content="Review dispatch outcome",
            trigger_condition="Next ego cycle",
            ego_source="user_ego_cycle",
            origin="system",
            proposal_id="abc12345deadbeef",
            max_cycles=3,
        )
        for _ in range(4):  # strict >: survives cycles 1-3, reaped at 4
            await ego_intentions.increment_unreviewed(db, "user_ego_cycle", [])
        expired = await ego_intentions.expire_overdue(db, "user_ego_cycle")
        assert expired == 1
        assert await ego_intentions.list_active(db, "user_ego_cycle") == []

    @pytest.mark.asyncio
    async def test_withdrawn_row_does_not_block_new_followthrough(self, db):
        """Dedup only considers ACTIVE rows — a reviewed/closed one clears the way."""
        from genesis.db.crud import ego_intentions

        iid = await ego_intentions.create(
            db,
            content="Review dispatch outcome",
            trigger_condition="Next ego cycle",
            ego_source="user_ego_cycle",
            origin="system",
            proposal_id="abc12345deadbeef",
        )
        await ego_intentions.withdraw(db, iid, ego_source="user_ego_cycle")
        assert not await ego_intentions.has_active_for_proposal(
            db, "user_ego_cycle", "abc12345deadbeef"
        )


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_sanitizes_intentions(self):
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "intentions": {
                "review": [
                    {"id": "abc", "action": "keep"},  # valid
                    {"id": "def", "action": "invalid"},  # invalid action
                    {"action": "fire"},  # missing id
                    "not a dict",  # not a dict
                ],
                "new": [
                    {"content": "X", "trigger_condition": "Y"},  # valid
                    {"content": "Z"},  # missing trigger_condition
                    "not a dict",
                ],
            },
        }
        result = _validate_output(data)
        assert result is not None
        assert len(result["intentions"]["review"]) == 1
        assert result["intentions"]["review"][0]["id"] == "abc"
        assert len(result["intentions"]["new"]) == 1

    def test_non_dict_intentions_normalized(self):
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "intentions": "not a dict",
        }
        result = _validate_output(data)
        assert result is not None
        assert result["intentions"] == {"review": [], "new": []}

    def test_no_intentions_is_fine(self):
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
        }
        result = _validate_output(data)
        assert result is not None
        assert "intentions" not in result

    def test_legacy_knowledge_updates_removed(self):
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "knowledge_updates": [
                {"section": "Open Questions", "action": "add", "content": "test"},
            ],
        }
        result = _validate_output(data)
        assert result is not None
        assert "knowledge_updates" not in result

    def test_renew_action_accepted(self):
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "intentions": {
                "review": [{"id": "abc", "action": "renew"}],
                "new": [],
            },
        }
        result = _validate_output(data)
        assert len(result["intentions"]["review"]) == 1


# ---------------------------------------------------------------------------
# Error-state visibility (WS-5): a query failure is a distinguishable marker,
# never silently rendered as the empty state.
# ---------------------------------------------------------------------------


class TestIntentionsSectionErrorMarker:
    @pytest.mark.asyncio
    async def test_query_error_renders_visible_marker(self, db, monkeypatch):
        from genesis.db.crud import ego_intentions
        from genesis.ego import intentions_context

        async def _boom(*a, **k):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(ego_intentions, "list_active", _boom)
        out = await intentions_context.build_intentions_section(db, "user_ego_cycle")
        assert "query error" in out.lower()
        assert out != ""  # not masked as the empty state
