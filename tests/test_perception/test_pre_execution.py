"""Tests for pre-execution assessment template rendering."""

from __future__ import annotations

from pathlib import Path


def test_template_loads():
    template_path = (
        Path(__file__).parent.parent.parent
        / "src" / "genesis" / "perception" / "templates"
        / "pre_execution_assessment.txt"
    )
    text = template_path.read_text()
    assert "Before executing this task" in text
    assert "{identity}" in text
    assert "{task_definition}" in text


def test_template_renders():
    template_path = (
        Path(__file__).parent.parent.parent
        / "src" / "genesis" / "perception" / "templates"
        / "pre_execution_assessment.txt"
    )
    text = template_path.read_text()
    rendered = text.format(
        identity="You are Genesis.",
        user_profile="Timezone: EST",
        cognitive_state="Working on Phase 4.",
        task_definition="Build the perception engine.",
    )
    assert "You are Genesis." in rendered
    assert "Build the perception engine." in rendered
    assert "{identity}" not in rendered
