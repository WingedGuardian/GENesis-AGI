"""Tests for the ego knowledge notepad — parse, apply, cap enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.ego.session import _parse_notepad_sections, _rebuild_notepad

# -- Fixtures ----------------------------------------------------------------

SAMPLE_NOTEPAD = """\
# Ego Notepad

> Maintained by the User Ego. Updated when observations warrant.
> Last updated: 2026-05-01

## Active Projects & Priorities
_(max 8 items)_

- [2026-05-01] Deep in v3.0b4 shipping sprint
- [2026-04-28] Portfolio restructuring to 7 projects

## Interests & Expertise
_(max 12 items)_

- [2026-04-25] Evaluating agent OS platforms competitively

## Interaction Patterns
_(max 8 items)_

## Proposal Context Journal
_(max 15 items)_

- [2026-05-03] REJECTED: LinkedIn outreach. Reason: mid-sprint. REOPEN WHEN: sprint ends.

## Open Questions
_(max 5 items)_
"""

EMPTY_NOTEPAD = """\
# Ego Notepad

> Maintained by the User Ego. Updated when observations warrant.
> Last updated: (not yet)

## Active Projects & Priorities
_(max 8 items)_

## Interests & Expertise
_(max 12 items)_

## Interaction Patterns
_(max 8 items)_

## Proposal Context Journal
_(max 15 items)_

## Open Questions
_(max 5 items)_
"""


# -- _parse_notepad_sections -------------------------------------------------


class TestParseNotepadSections:
    def test_parses_all_sections(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        # 5 real sections + __header__
        assert "Active Projects & Priorities" in sections
        assert "Interests & Expertise" in sections
        assert "Interaction Patterns" in sections
        assert "Proposal Context Journal" in sections
        assert "Open Questions" in sections
        assert "__header__" in sections

    def test_parses_entries(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        entries = sections["Active Projects & Priorities"]["entries"]
        assert len(entries) == 2
        assert "v3.0b4" in entries[0]

    def test_parses_caps(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        assert sections["Active Projects & Priorities"]["cap"] == 8
        assert sections["Interests & Expertise"]["cap"] == 12
        assert sections["Open Questions"]["cap"] == 5

    def test_empty_section_has_no_entries(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        assert sections["Interaction Patterns"]["entries"] == []
        assert sections["Open Questions"]["entries"] == []

    def test_header_preserved(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        header = sections["__header__"]["entries"]
        assert any("Ego Notepad" in line for line in header)

    def test_empty_notepad(self):
        sections = _parse_notepad_sections(EMPTY_NOTEPAD)
        assert len(sections["Active Projects & Priorities"]["entries"]) == 0
        assert sections["Active Projects & Priorities"]["cap"] == 8


# -- _rebuild_notepad --------------------------------------------------------


class TestRebuildNotepad:
    def test_roundtrip_preserves_content(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        result = _rebuild_notepad(sections, "2026-05-07")
        # Should contain all original entries
        assert "v3.0b4" in result
        assert "LinkedIn outreach" in result
        assert "agent OS platforms" in result

    def test_updates_timestamp(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        result = _rebuild_notepad(sections, "2026-05-07")
        assert "Last updated: 2026-05-07" in result
        assert "Last updated: 2026-05-01" not in result

    def test_sections_in_defined_order(self):
        sections = _parse_notepad_sections(SAMPLE_NOTEPAD)
        result = _rebuild_notepad(sections, "2026-05-07")
        pos_projects = result.index("Active Projects")
        pos_interests = result.index("Interests & Expertise")
        pos_questions = result.index("Open Questions")
        assert pos_projects < pos_interests < pos_questions
        # Proposal Context Journal is a defined section, ordered before Open Questions.
        if "Proposal Context Journal" in result:
            pos_journal = result.index("Proposal Context Journal")
            assert pos_interests < pos_journal < pos_questions
        # Removed sections (Interaction Patterns) are treated as extras
        # and appended after defined sections.
        if "Interaction Patterns" in result:
            pos_patterns = result.index("Interaction Patterns")
            assert pos_patterns > pos_questions


# -- _apply_knowledge_updates (integration via EgoSession) -------------------


class TestApplyKnowledgeUpdates:
    """Test the full apply flow by simulating what EgoSession._apply_knowledge_updates does."""

    @pytest.fixture()
    def notepad_path(self, tmp_path: Path) -> Path:
        """Create a temporary notepad file."""
        p = tmp_path / "EGO_NOTEPAD.md"
        p.write_text(SAMPLE_NOTEPAD)
        return p

    def _apply(self, path: Path, updates: list[dict]) -> str:
        """Simulate _apply_knowledge_updates logic without the full EgoSession."""
        from datetime import UTC, datetime

        text = path.read_text()
        sections = _parse_notepad_sections(text)
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        for u in updates:
            section_name = u["section"]
            action = u["action"]
            content = u["content"]

            if section_name not in sections:
                continue

            entries = sections[section_name]["entries"]
            cap = sections[section_name]["cap"]

            if action == "add":
                entries.append(f"- [{today}] {content}")
                if cap and len(entries) > cap:
                    entries[:] = entries[-cap:]
            elif action == "update":
                replaces = u.get("replaces", "")
                if not replaces:
                    continue
                for i, entry in enumerate(entries):
                    if replaces in entry:
                        entries[i] = f"- [{today}] {content}"
                        break
            elif action == "remove":
                for i, entry in enumerate(entries):
                    if content in entry:
                        entries.pop(i)
                        break

        result = _rebuild_notepad(sections, today)
        path.write_text(result)
        return result

    def test_add_entry(self, notepad_path: Path):
        result = self._apply(notepad_path, [
            {"section": "Open Questions", "action": "add",
             "content": "What is Jay's freelance income target?"},
        ])
        assert "freelance income target" in result

    def test_add_to_existing_section(self, notepad_path: Path):
        result = self._apply(notepad_path, [
            {"section": "Active Projects & Priorities", "action": "add",
             "content": "Ego notepad feature in progress"},
        ])
        # Should have 3 entries now (2 original + 1 new)
        sections = _parse_notepad_sections(result)
        assert len(sections["Active Projects & Priorities"]["entries"]) == 3

    def test_remove_entry(self, notepad_path: Path):
        result = self._apply(notepad_path, [
            {"section": "Active Projects & Priorities", "action": "remove",
             "content": "v3.0b4"},
        ])
        assert "v3.0b4" not in result
        sections = _parse_notepad_sections(result)
        assert len(sections["Active Projects & Priorities"]["entries"]) == 1

    def test_update_entry(self, notepad_path: Path):
        result = self._apply(notepad_path, [
            {"section": "Active Projects & Priorities", "action": "update",
             "content": "Deep in v3.0b5 release",
             "replaces": "v3.0b4"},
        ])
        assert "v3.0b5" in result
        assert "v3.0b4" not in result

    def test_cap_enforcement(self, notepad_path: Path):
        """Adding beyond cap should trim oldest entries."""
        # Open Questions has cap=5. Add 6 items to empty section.
        updates = [
            {"section": "Open Questions", "action": "add",
             "content": f"Question {i}"}
            for i in range(6)
        ]
        result = self._apply(notepad_path, updates)
        sections = _parse_notepad_sections(result)
        entries = sections["Open Questions"]["entries"]
        assert len(entries) == 5
        # Oldest (Question 0) should be trimmed
        assert "Question 0" not in "\n".join(entries)
        assert "Question 5" in "\n".join(entries)

    def test_unknown_section_ignored(self, notepad_path: Path):
        """Updates to nonexistent sections should be silently skipped."""
        self._apply(notepad_path, [
            {"section": "Nonexistent Section", "action": "add",
             "content": "Should not appear"},
        ])
        # Content shouldn't be modified (except timestamp)
        result = notepad_path.read_text()
        assert "Nonexistent Section" not in result
        # Original entries should still be there
        assert "v3.0b4" in result

    def test_update_with_missing_replaces_is_noop(self, notepad_path: Path):
        """Update without replaces field should be a no-op."""
        original_sections = _parse_notepad_sections(notepad_path.read_text())
        original_count = len(original_sections["Active Projects & Priorities"]["entries"])

        self._apply(notepad_path, [
            {"section": "Active Projects & Priorities", "action": "update",
             "content": "New content"},
        ])
        result_sections = _parse_notepad_sections(notepad_path.read_text())
        assert len(result_sections["Active Projects & Priorities"]["entries"]) == original_count

    def test_remove_nonexistent_is_noop(self, notepad_path: Path):
        """Removing content that doesn't exist should not crash."""
        self._apply(notepad_path, [
            {"section": "Active Projects & Priorities", "action": "remove",
             "content": "This entry does not exist"},
        ])
        sections = _parse_notepad_sections(notepad_path.read_text())
        # Original entries untouched
        assert len(sections["Active Projects & Priorities"]["entries"]) == 2


# -- Validation tests --------------------------------------------------------


class TestValidateKnowledgeUpdates:
    def test_malformed_updates_filtered(self):
        """_validate_output should filter out malformed knowledge_updates."""
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "knowledge_updates": [
                # Valid
                {"section": "Open Questions", "action": "add", "content": "What?"},
                # Missing content
                {"section": "Open Questions", "action": "add"},
                # Invalid action
                {"section": "Open Questions", "action": "destroy", "content": "X"},
                # Not a dict
                "just a string",
                # Missing section
                {"action": "add", "content": "Y"},
            ],
        }
        result = _validate_output(data)
        assert result is not None
        assert len(result["knowledge_updates"]) == 1
        assert result["knowledge_updates"][0]["content"] == "What?"

    def test_no_knowledge_updates_is_fine(self):
        """Output without knowledge_updates should pass validation."""
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
        }
        result = _validate_output(data)
        assert result is not None
        assert "knowledge_updates" not in result

    def test_non_list_knowledge_updates_cleared(self):
        """Non-list knowledge_updates should be replaced with empty list."""
        from genesis.ego.session import _validate_output

        data = {
            "proposals": [],
            "focus_summary": "test",
            "follow_ups": [],
            "knowledge_updates": "not a list",
        }
        result = _validate_output(data)
        assert result is not None
        assert result["knowledge_updates"] == []
