"""Tests for the skill-golden-set authoring helper — scaffold + validate."""

from __future__ import annotations

from genesis.eval.skill_golden_set import validate, write_scaffold


def test_scaffold_writes_loadable_suite(tmp_path):
    path = tmp_path / "myskill.jsonl"
    write_scaffold("myskill", path, force=False)
    assert path.exists()
    # It loads cleanly through the bench task loader (tmp_path is outside the repo).
    assert validate(path) == 0


def test_scaffold_refuses_overwrite_without_force(tmp_path):
    path = tmp_path / "s.jsonl"
    write_scaffold("s", path, force=False)
    with __import__("pytest").raises(SystemExit):
        write_scaffold("s", path, force=False)
    # force overwrites.
    write_scaffold("s", path, force=True)


def test_validate_missing_file_returns_error(tmp_path):
    assert validate(tmp_path / "nope.jsonl") == 1


def test_validate_flags_placeholders(tmp_path, capsys):
    path = tmp_path / "ph.jsonl"
    write_scaffold("ph", path, force=False)
    assert validate(path) == 0
    assert "placeholder" in capsys.readouterr().out.lower()
