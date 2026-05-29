"""Tests for post-dispatch output verification."""

from __future__ import annotations

import json
from pathlib import Path

from genesis.ego.verification import (
    ExpectedOutputs,
    parse_expected_outputs,
    verify_outputs,
)

# --- parse_expected_outputs ---


def test_parse_none():
    assert parse_expected_outputs(None) is None


def test_parse_empty_string():
    assert parse_expected_outputs("") is None


def test_parse_non_json_string():
    assert parse_expected_outputs("background CC session, ~$0.50") is None


def test_parse_malformed_json():
    assert parse_expected_outputs("{bad json}") is None


def test_parse_json_without_files():
    """JSON without 'files' key should return None."""
    assert parse_expected_outputs(json.dumps({"min_size_bytes": 100})) is None


def test_parse_json_with_empty_files():
    """Empty files list should return None."""
    assert parse_expected_outputs(json.dumps({"files": []})) is None


def test_parse_valid_minimal():
    """Minimal valid expected_outputs with only files."""
    raw = json.dumps({"files": ["/tmp/test.md"]})
    result = parse_expected_outputs(raw)
    assert result is not None
    assert result.files == ["/tmp/test.md"]
    assert result.min_size_bytes == 0
    assert result.required_strings == []


def test_parse_valid_full():
    """Full expected_outputs with all fields."""
    raw = json.dumps({
        "files": ["/tmp/a.md", "/tmp/b.md"],
        "min_size_bytes": 500,
        "required_strings": ["## Summary", "## Conclusion"],
    })
    result = parse_expected_outputs(raw)
    assert result is not None
    assert result.files == ["/tmp/a.md", "/tmp/b.md"]
    assert result.min_size_bytes == 500
    assert result.required_strings == ["## Summary", "## Conclusion"]


# --- verify_outputs ---


def test_verify_all_pass(tmp_path: Path):
    """Files exist, right size, required strings present."""
    f = tmp_path / "output.md"
    f.write_text("## Summary\nThis is the output.\n## Conclusion\nDone.")
    expected = ExpectedOutputs(
        files=[str(f)],
        min_size_bytes=10,
        required_strings=["## Summary"],
    )
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.failures == []


def test_verify_missing_file(tmp_path: Path):
    """Missing file produces a failure."""
    expected = ExpectedOutputs(files=[str(tmp_path / "nonexistent.md")])
    result = verify_outputs(expected)
    assert result.passed is False
    assert len(result.failures) == 1
    assert "Missing file" in result.failures[0]


def test_verify_file_too_small(tmp_path: Path):
    """File exists but under min_size_bytes."""
    f = tmp_path / "small.md"
    f.write_text("hi")
    expected = ExpectedOutputs(files=[str(f)], min_size_bytes=1000)
    result = verify_outputs(expected)
    assert result.passed is False
    assert "too small" in result.failures[0].lower()


def test_verify_missing_required_string(tmp_path: Path):
    """File exists, right size, but missing a required string."""
    f = tmp_path / "output.md"
    f.write_text("## Introduction\nSome content here.\n")
    expected = ExpectedOutputs(
        files=[str(f)],
        min_size_bytes=5,
        required_strings=["## Summary"],
    )
    result = verify_outputs(expected)
    assert result.passed is False
    assert "Missing required string" in result.failures[0]


def test_verify_multiple_files(tmp_path: Path):
    """Multiple files: one exists, one doesn't."""
    good = tmp_path / "good.md"
    good.write_text("content")
    expected = ExpectedOutputs(
        files=[str(good), str(tmp_path / "missing.md")],
    )
    result = verify_outputs(expected)
    assert result.passed is False
    assert len(result.failures) == 1
    assert "missing.md" in result.failures[0]


def test_verify_no_files():
    """Empty files list passes vacuously."""
    expected = ExpectedOutputs(files=[])
    result = verify_outputs(expected)
    assert result.passed is True
