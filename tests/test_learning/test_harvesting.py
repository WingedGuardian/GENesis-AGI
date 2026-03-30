"""Tests for harvesting modules: debrief and auto_memory."""

from __future__ import annotations

import json
from pathlib import Path

from genesis.learning.harvesting.auto_memory import harvest_auto_memory
from genesis.learning.harvesting.debrief import parse_debrief

# ── debrief tests ──


class TestParseDebrief:
    def test_json_format(self):
        text = json.dumps({"learnings": ["lesson1", "lesson2"]})
        assert parse_debrief(text) == ["lesson1", "lesson2"]

    def test_json_embedded_in_text(self):
        text = 'Some preamble\n{"learnings": ["embedded"]}\ntrailing'
        assert parse_debrief(text) == ["embedded"]

    def test_markdown_format(self):
        text = "# Summary\nDone.\n## Learnings\n- lesson A\n- lesson B\n\n## Other\nStuff."
        assert parse_debrief(text) == ["lesson A", "lesson B"]

    def test_markdown_bullet_star(self):
        text = "## Learnings\n* star bullet\n* another"
        assert parse_debrief(text) == ["star bullet", "another"]

    def test_no_learnings(self):
        assert parse_debrief("Just some random text") == []

    def test_empty_string(self):
        assert parse_debrief("") == []

    def test_malformed_json(self):
        text = '{"learnings": "not a list"}'
        assert parse_debrief(text) == []

    def test_json_preferred_over_markdown(self):
        text = '{"learnings": ["from json"]}\n## Learnings\n- from md'
        assert parse_debrief(text) == ["from json"]


# ── auto_memory tests ──


class TestHarvestAutoMemory:
    def test_nonexistent_dir(self, tmp_path: Path):
        assert harvest_auto_memory(tmp_path / "nope") == []

    def test_relevant_file(self, tmp_path: Path):
        (tmp_path / "notes.md").write_text("Genesis routing insight")
        items = harvest_auto_memory(tmp_path)
        assert len(items) == 1
        assert items[0]["file"] == "notes.md"
        assert "routing insight" in items[0]["content"]

    def test_filters_cc_internal_session(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("# Claude Code Session Notes\nstuff")
        assert harvest_auto_memory(tmp_path) == []

    def test_filters_cc_internal_context_window(self, tmp_path: Path):
        (tmp_path / "b.md").write_text("Remember the context window limit")
        assert harvest_auto_memory(tmp_path) == []

    def test_filters_cc_internal_session_heading(self, tmp_path: Path):
        (tmp_path / "c.md").write_text("# Session Log\ndetails")
        assert harvest_auto_memory(tmp_path) == []

    def test_non_md_ignored(self, tmp_path: Path):
        (tmp_path / "notes.txt").write_text("Genesis relevant")
        assert harvest_auto_memory(tmp_path) == []

    def test_custom_exclude(self, tmp_path: Path):
        import re

        (tmp_path / "a.md").write_text("EXCLUDE ME")
        patterns = [re.compile(r"EXCLUDE ME")]
        assert harvest_auto_memory(tmp_path, exclude_patterns=patterns) == []

    def test_mixed(self, tmp_path: Path):
        (tmp_path / "internal.md").write_text("# Claude Code Internals\nstuff")
        (tmp_path / "good.md").write_text("Useful architecture note")
        (tmp_path / "also_good.md").write_text("Another relevant item")
        items = harvest_auto_memory(tmp_path)
        assert len(items) == 2
        names = {i["file"] for i in items}
        assert names == {"good.md", "also_good.md"}
