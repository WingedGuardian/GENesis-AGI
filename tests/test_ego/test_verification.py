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
    raw = json.dumps(
        {
            "files": ["/tmp/a.md", "/tmp/b.md"],
            "min_size_bytes": 500,
            "required_strings": ["## Summary", "## Conclusion"],
        }
    )
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
    assert result.missing_files == []
    assert result.advisories == []


def test_verify_missing_file(tmp_path: Path):
    """Missing file is a hard failure (the deliverable was not produced)."""
    expected = ExpectedOutputs(files=[str(tmp_path / "nonexistent.md")])
    result = verify_outputs(expected)
    assert result.passed is False
    assert len(result.missing_files) == 1
    assert "Missing file" in result.missing_files[0]


def test_verify_file_too_small_is_advisory(tmp_path: Path):
    """A non-empty file under min_size_bytes is an advisory, NOT a hard fail.

    Size is a weak proxy — a shorter-than-expected but real deliverable still
    succeeded; we surface a note but never fail on it.
    """
    f = tmp_path / "small.md"
    f.write_text("hi")  # non-empty, but tiny
    expected = ExpectedOutputs(files=[str(f)], min_size_bytes=1000)
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.missing_files == []
    assert any("small" in a.lower() for a in result.advisories)


def test_verify_directory_is_not_a_deliverable(tmp_path: Path):
    """A directory at the expected path is not a produced file → hard fail
    (guards against st_size on a dir reading as a non-empty 'file')."""
    d = tmp_path / "output.md"  # a directory sitting where a file was expected
    d.mkdir()
    (d / "child.txt").write_text("x")  # non-empty dir → st_size != 0 (exposes the bug)
    expected = ExpectedOutputs(files=[str(d)])
    result = verify_outputs(expected)
    assert result.passed is False
    assert result.missing_files


def test_verify_empty_file_is_hard_fail(tmp_path: Path):
    """A 0-byte file counts as 'not produced' — a hard failure."""
    f = tmp_path / "empty.md"
    f.write_text("")
    expected = ExpectedOutputs(files=[str(f)])
    result = verify_outputs(expected)
    assert result.passed is False
    assert len(result.missing_files) == 1
    assert "empty" in result.missing_files[0].lower()


def test_verify_missing_required_string_is_advisory(tmp_path: Path):
    """A missing required string is advisory only — the deliverable exists.

    This is the core regression lock: a string miss is a failure of the
    matcher, NOT of the deliverable, and must NEVER hard-fail the proposal.
    """
    f = tmp_path / "output.md"
    f.write_text("## Introduction\nSome content here.\n")
    expected = ExpectedOutputs(
        files=[str(f)],
        min_size_bytes=5,
        required_strings=["## Summary"],
    )
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.missing_files == []
    assert any("## Summary" in a for a in result.advisories)


def test_verify_multiple_files(tmp_path: Path):
    """Multiple files: one exists, one doesn't → hard fail on the missing one."""
    good = tmp_path / "good.md"
    good.write_text("content")
    expected = ExpectedOutputs(
        files=[str(good), str(tmp_path / "missing.md")],
    )
    result = verify_outputs(expected)
    assert result.passed is False
    assert len(result.missing_files) == 1
    assert "missing.md" in result.missing_files[0]


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


def test_verify_fuzzy_match_passes_but_flagged(tmp_path: Path):
    """Missing exact file but fuzzy match found — passes (never false-fails a
    real rename), but the fuzzy substitution is flagged as an advisory so a
    possibly-wrong file never passes silently."""
    actual = tmp_path / "arxiv-related-work-draft-v1.md"
    actual.write_text("## Related Work\nGenesis is a cognitive architecture.")
    expected = ExpectedOutputs(
        files=[str(tmp_path / "arxiv-related-work-draft.md")],
        min_size_bytes=10,
        required_strings=["## Related Work"],
    )
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.missing_files == []
    # Content matched, but the fuzzy substitution itself is still surfaced.
    assert any("fuzzy filename match" in a.lower() for a in result.advisories)


def test_verify_fuzzy_match_never_silent(tmp_path: Path):
    """Any fuzzy substitution is surfaced as an advisory, even when the content
    check also passes — a possibly-wrong similarly-named file must never mark a
    proposal produced silently. No heuristic (name OR content) reliably
    separates a rename from an unrelated file, so we flag rather than gate.
    """
    actual = tmp_path / "report-v2.md"
    actual.write_text("Some reworded content without the exact heading.")
    expected = ExpectedOutputs(
        files=[str(tmp_path / "report.md")],
        required_strings=["## Summary"],
    )
    result = verify_outputs(expected)
    # Still passes (never a false-fail on a possible rename)...
    assert result.passed is True
    assert result.missing_files == []
    # ...but the fuzzy substitution is loudly flagged for verification.
    assert any("fuzzy filename match" in a.lower() for a in result.advisories)
    assert any("report-v2.md" in a for a in result.advisories)


def test_verify_fuzzy_partial_string_match_accepted(tmp_path: Path):
    """A fuzzy rename that shares AT LEAST ONE anchor is accepted; the missing
    anchors are advisory only (it is the deliverable, just partially reworded)."""
    actual = tmp_path / "report-v2.md"
    actual.write_text("## Summary\nReal reworded body.\n")
    expected = ExpectedOutputs(
        files=[str(tmp_path / "report.md")],
        required_strings=["## Summary", "## Appendix"],  # one present, one absent
    )
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.missing_files == []
    assert any("## Appendix" in a for a in result.advisories)


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


# --- path expansion (Class B: ~ and $VARS were never expanded) ---


def test_verify_expands_tilde_path(tmp_path: Path, monkeypatch):
    """A ``~/...`` file path must be expanded before the existence check.

    Regression lock: ``Path("~/x.md").exists()`` is ALWAYS False (Python does
    not expand ``~``), which produced spurious 'Missing file' hard-fails on
    real deliverables written to the expanded home path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "report.md").write_text("## Summary\nReal deliverable.\n")
    expected = ExpectedOutputs(
        files=["~/report.md"],
        min_size_bytes=5,
        required_strings=["## Summary"],
    )
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.missing_files == []
    assert result.advisories == []


def test_verify_expands_env_var_path(tmp_path: Path, monkeypatch):
    """A ``$VAR/...`` file path must be expanded before the existence check."""
    monkeypatch.setenv("GENESIS_OUT", str(tmp_path))
    (tmp_path / "report.md").write_text("content here")
    expected = ExpectedOutputs(files=["$GENESIS_OUT/report.md"])
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.missing_files == []


# --- forgiving (advisory-only) string matching (Class A) ---


def test_verify_required_string_case_insensitive(tmp_path: Path):
    """Required-string match is case-insensitive — no advisory on casing diff."""
    f = tmp_path / "output.md"
    f.write_text("## confirm with ram before building\n")
    expected = ExpectedOutputs(files=[str(f)], required_strings=["Confirm With Ram"])
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.advisories == []


def test_verify_required_string_whitespace_normalized(tmp_path: Path):
    """Internal whitespace differences don't trip the matcher."""
    f = tmp_path / "output.md"
    f.write_text("##   Summary\nbody\n")  # extra spaces
    expected = ExpectedOutputs(files=[str(f)], required_strings=["## Summary"])
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.advisories == []


def test_verify_content_miss_never_hard_fails(tmp_path: Path):
    """SOC-blueprint class: a deliverable that addressed intent via different
    phrasing (e.g. '[CONFIRM-RAM]' + a question list, not the literal
    'Confirm with Ram') gets an advisory but NEVER a hard fail."""
    f = tmp_path / "wf-blueprint.md"
    f.write_text(
        "## 7. Confirm-before-building — the question list for Ram\n"
        "Every [CONFIRM-RAM] guess above is resolved by answering these.\n"
    )
    expected = ExpectedOutputs(
        files=[str(f)],
        min_size_bytes=10,
        required_strings=["Confirm with Ram"],
    )
    result = verify_outputs(expected)
    assert result.passed is True
    assert result.missing_files == []
    assert any("Confirm with Ram" in a for a in result.advisories)
