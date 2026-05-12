"""Tests for the ego world model components: goals, contacts, world snapshot."""

import json

import pytest

from genesis.db.crud import memory_events, user_contacts, user_goals
from genesis.ego.world_snapshot import WorldSnapshot
from genesis.ego.world_snapshot import build as build_snapshot
from genesis.memory.contact_tracker import _find_best_contact_match, _is_likely_person
from genesis.memory.extraction import Extraction
from genesis.memory.goal_tracker import _detect_goal_signal

# -- Goal CRUD tests --


class TestUserGoalsCRUD:
    @pytest.mark.asyncio
    async def test_create_and_list_active(self, db):
        goal_id = await user_goals.create(
            db, title="Build thought leadership", category="career",
            priority="high", confidence=0.8,
        )
        assert goal_id
        active = await user_goals.list_active(db)
        assert len(active) == 1
        assert active[0]["title"] == "Build thought leadership"
        assert active[0]["priority"] == "high"

    @pytest.mark.asyncio
    async def test_priority_ordering(self, db):
        await user_goals.create(db, title="Low goal", category="other", priority="low")
        await user_goals.create(db, title="Critical goal", category="career", priority="critical")
        await user_goals.create(db, title="Medium goal", category="project", priority="medium")
        active = await user_goals.list_active(db)
        assert active[0]["title"] == "Critical goal"
        assert active[1]["title"] == "Medium goal"
        assert active[2]["title"] == "Low goal"

    @pytest.mark.asyncio
    async def test_mark_achieved(self, db):
        gid = await user_goals.create(db, title="Ship v1", category="project")
        await user_goals.mark_achieved(db, gid)
        g = await user_goals.get_by_id(db, gid)
        assert g["status"] == "achieved"
        assert g["achieved_at"] is not None

    @pytest.mark.asyncio
    async def test_add_progress_note(self, db):
        gid = await user_goals.create(db, title="Learn Rust", category="learning")
        await user_goals.add_progress_note(db, gid, "Completed chapter 3")
        g = await user_goals.get_by_id(db, gid)
        notes = json.loads(g["progress_notes"])
        assert len(notes) == 1
        assert "chapter 3" in notes[0]["note"]

    @pytest.mark.asyncio
    async def test_find_similar(self, db):
        await user_goals.create(db, title="Build thought leadership in AGI", category="career")
        match = await user_goals.find_similar(db, "Build thought leadership in AGI space")
        assert match is not None
        assert "thought leadership" in match["title"]

    @pytest.mark.asyncio
    async def test_find_similar_no_match(self, db):
        await user_goals.create(db, title="Build thought leadership", category="career")
        match = await user_goals.find_similar(db, "learn to cook pasta")
        assert match is None

    @pytest.mark.asyncio
    async def test_list_by_category(self, db):
        await user_goals.create(db, title="Career goal", category="career")
        await user_goals.create(db, title="Project goal", category="project")
        career = await user_goals.list_by_category(db, "career")
        assert len(career) == 1
        assert career[0]["category"] == "career"


# -- Contact CRUD tests --


class TestUserContactsCRUD:
    @pytest.mark.asyncio
    async def test_create_and_list(self, db):
        cid = await user_contacts.create(db, name="John Smith", organization="Acme")
        assert cid
        contacts = await user_contacts.list_all(db)
        assert len(contacts) == 1
        assert contacts[0]["name"] == "John Smith"

    @pytest.mark.asyncio
    async def test_find_by_name(self, db):
        await user_contacts.create(db, name="Jane Doe", relationship="colleague")
        found = await user_contacts.find_by_name(db, "jane doe")
        assert found is not None
        assert found["name"] == "Jane Doe"

    @pytest.mark.asyncio
    async def test_record_mention(self, db):
        cid = await user_contacts.create(db, name="Bob Wilson")
        await user_contacts.record_mention(db, cid, context="Discussed project timeline")
        c = await user_contacts.get_by_id(db, cid)
        assert c["interaction_count"] == 2  # 1 initial + 1 mention
        notes = json.loads(c["context_notes"])
        assert len(notes) == 1
        assert "project timeline" in notes[0]["context"]

    @pytest.mark.asyncio
    async def test_recently_active(self, db):
        await user_contacts.create(db, name="Recent Contact")
        active = await user_contacts.recently_active(db, days=1)
        assert len(active) == 1


# -- Goal tracker tests --


class TestGoalTracker:
    def test_detect_goal_signal_positive(self):
        ext = Extraction(
            content="User wants to establish career thought leadership in AI space",
            extraction_type="preference",
            confidence=0.9,
        )
        signal = _detect_goal_signal(ext)
        assert signal is not None
        assert signal["category"] == "career"

    def test_detect_goal_signal_low_confidence(self):
        ext = Extraction(
            content="User might want to learn cooking",
            extraction_type="entity",
            confidence=0.3,
        )
        assert _detect_goal_signal(ext) is None

    def test_detect_goal_signal_no_keywords(self):
        ext = Extraction(
            content="Genesis uses SQLite for storage",
            extraction_type="entity",
            confidence=0.9,
        )
        assert _detect_goal_signal(ext) is None


# -- Contact tracker tests --


class TestContactTracker:
    def test_is_likely_person_positive(self):
        assert _is_likely_person("John Smith")
        assert _is_likely_person("Jane Doe")
        assert _is_likely_person("Dr Emily Chen")

    def test_is_likely_person_negative(self):
        assert not _is_likely_person("Genesis")
        assert not _is_likely_person("Python")
        assert not _is_likely_person("PR #123")
        assert not _is_likely_person("src/genesis/ego/session.py")
        assert not _is_likely_person("A")  # Too short

    def test_find_best_contact_match(self):
        contacts = [
            {"name": "John Smith", "id": "1"},
            {"name": "Jane Doe", "id": "2"},
        ]
        assert _find_best_contact_match("John Smith", contacts) is not None
        assert _find_best_contact_match("john smith", contacts) is not None  # case-insensitive
        assert _find_best_contact_match("Bob Wilson", contacts) is None


# -- World snapshot tests --


class TestWorldSnapshot:
    def test_render_empty(self):
        snapshot = WorldSnapshot()
        rendered = snapshot.render()
        assert "No world model data yet" in rendered

    def test_render_with_goals(self):
        snapshot = WorldSnapshot(
            goals=[{
                "title": "Build thought leadership",
                "priority": "high",
                "category": "career",
                "timeline": "Q2 2026",
                "progress_notes": "[]",
            }],
        )
        rendered = snapshot.render()
        assert "Active Goals" in rendered
        assert "Build thought leadership" in rendered
        assert "HIGH" in rendered

    def test_render_with_events(self):
        snapshot = WorldSnapshot(
            upcoming_events=[{
                "subject": "user",
                "verb": "registered",
                "object": "AI Conference",
                "event_date": "2099-06-15T00:00:00+00:00",
            }],
        )
        rendered = snapshot.render()
        assert "Upcoming Events" in rendered
        assert "AI Conference" in rendered

    def test_render_with_contacts(self):
        snapshot = WorldSnapshot(
            active_contacts=[{
                "name": "John Smith",
                "organization": "Acme Corp",
                "relationship": "colleague",
                "interaction_count": 5,
            }],
        )
        rendered = snapshot.render()
        assert "Recently Active Contacts" in rendered
        assert "John Smith" in rendered
        assert "Acme Corp" in rendered

    @pytest.mark.asyncio
    async def test_build_snapshot_empty_db(self, db):
        snapshot = await build_snapshot(db)
        assert snapshot.goals == []
        assert snapshot.upcoming_events == []
        assert snapshot.active_contacts == []


# -- Memory events extended CRUD --


class TestMemoryEventsExtended:
    @pytest.mark.asyncio
    async def test_upcoming_user_events(self, db):
        # Insert a future user event
        await memory_events.insert(
            db, memory_id="mem1", subject="user", verb="registered",
            object_="AI Conference", event_date="2099-06-15",
        )
        # Insert a Genesis system event (should be excluded)
        await memory_events.insert(
            db, memory_id="mem2", subject="Genesis", verb="deployed",
            object_="v3.1", event_date="2099-06-15",
        )
        upcoming = await memory_events.upcoming_user_events(db, days=365*100)
        assert len(upcoming) == 1
        assert upcoming[0]["subject"] == "user"

    @pytest.mark.asyncio
    async def test_approaching_deadlines(self, db):
        await memory_events.insert(
            db, memory_id="mem3", subject="user", verb="deadline",
            object_="Paper submission", event_date="2099-01-01",
        )
        deadlines = await memory_events.approaching_deadlines(db, days=365*100)
        assert len(deadlines) == 1
