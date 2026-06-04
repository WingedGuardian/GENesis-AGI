"""Tests for post-dispatch output verification."""

from __future__ import annotations

import json
import time
from pathlib import Path

from genesis.ego.verification import (
    ExpectedOutputs,
    _find_similar,
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


# --- _find_similar (fuzzy matching) ---


def test_find_similar_suffix_added(tmp_path: Path):
    """Expected 'foo.md', actual 'foo-v1.md' — should match."""
    actual = tmp_path / "foo-v1.md"
    actual.write_text("content")
    expected = tmp_path / "foo.md"
    match = _find_similar(expected)
    assert match == actual


def test_find_similar_name_variation(tmp_path: Path):
    """Expected 'social-posts-article-5.md', actual with added words."""
    actual = tmp_path / "social-posts-article5-stateless.md"
    actual.write_text("content")
    expected = tmp_path / "social-posts-article-5.md"
    match = _find_similar(expected)
    assert match == actual


def test_find_similar_no_match(tmp_path: Path):
    """Completely different filename should not match."""
    actual = tmp_path / "master-marketing-plan.md"
    actual.write_text("content")
    expected = tmp_path / "arxiv-related-work-draft.md"
    match = _find_similar(expected)
    assert match is None


def test_find_similar_wrong_extension(tmp_path: Path):
    """Same stem but different extension should not match."""
    actual = tmp_path / "report.json"
    actual.write_text("{}")
    expected = tmp_path / "report.md"
    match = _find_similar(expected)
    assert match is None


def test_find_similar_prefers_recency(tmp_path: Path):
    """When two candidates have same ratio, prefer most recent."""

    old_file = tmp_path / "report-old.md"
    old_file.write_text("old content")
    # Ensure different mtime
    time.sleep(0.05)
    new_file = tmp_path / "report-new.md"
    new_file.write_text("new content")

    expected = tmp_path / "report.md"
    match = _find_similar(expected)
    # Both score ~0.7-0.8 against "report", but new_file is more recent
    assert match is not None
    assert match == new_file


def test_find_similar_empty_dir(tmp_path: Path):
    """Empty directory returns None."""
    expected = tmp_path / "anything.md"
    match = _find_similar(expected)
    assert match is None


def test_find_similar_nonexistent_parent():
    """Non-existent parent directory returns None."""
    expected = Path("/nonexistent/dir/file.md")
    match = _find_similar(expected)
    assert match is None


# --- verify_outputs with fuzzy matching ---


def test_verify_fuzzy_match_passes(tmp_path: Path):
    """Missing exact file but fuzzy match found — should pass."""
    actual = tmp_path / "arxiv-related-work-draft-v1.md"
    actual.write_text("## Related Work\nGenesis is a cognitive architecture.")
    expected = ExpectedOutputs(
        files=[str(tmp_path / "arxiv-related-work-draft.md")],
        min_size_bytes=10,
        required_strings=["## Related Work"],
    )
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.failures == []


def test_verify_fuzzy_match_checks_content(tmp_path: Path):
    """Fuzzy-matched file still must pass content checks."""
    actual = tmp_path / "report-v2.md"
    actual.write_text("Some content without the required heading.")
    expected = ExpectedOutputs(
        files=[str(tmp_path / "report.md")],
        required_strings=["## Summary"],
    )
    result = verify_outputs(expected)
    assert result.passed is False
    assert any("Missing required string" in f for f in result.failures)
    # Failure message should reference the fuzzy-matched path for debugging
    assert any("fuzzy match" in f for f in result.failures)


def test_verify_no_fuzzy_for_exact_match(tmp_path: Path):
    """When exact file exists, fuzzy matching is not attempted."""
    exact = tmp_path / "output.md"
    exact.write_text("correct content")
    # Also create a similarly-named file
    decoy = tmp_path / "output-v2.md"
    decoy.write_text("decoy content")
    expected = ExpectedOutputs(files=[str(exact)])
    result = verify_outputs(expected)
    assert result.passed is True
