"""Tests for inbox response writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.inbox.writer import ResponseWriter


@pytest.fixture
def writer(tmp_path: Path) -> ResponseWriter:
    return ResponseWriter(watch_path=tmp_path, timezone="America/New_York")


@pytest.mark.asyncio
async def test_single_item_sibling_file(writer: ResponseWriter, tmp_path: Path):
    """Single-item batch produces a sibling .genesis.md file."""
    source = tmp_path / "Untitled.md"
    source.write_text("test content")
    path = await writer.write_response(
        batch_id="abc12345-6789",
        source_files=[str(source)],
        evaluation_text="# Evaluation\n\nLooks good.",
        item_count=1,
    )
    assert path.exists()
    assert path.name == "Untitled-1.genesis.md"
    assert path.parent == tmp_path
    content = path.read_text()
    assert "Looks good" in content


@pytest.mark.asyncio
async def test_multi_item_batch_uses_batch_id(writer: ResponseWriter, tmp_path: Path):
    """Multi-item batch uses date-based batch filename."""
    path = await writer.write_response(
        batch_id="batch123-xyz",
        source_files=["a.md", "b.md"],
        evaluation_text="# Eval",
        item_count=2,
    )
    assert path.exists()
    assert path.name.endswith(".genesis.md")
    assert "batch123" in path.name


@pytest.mark.asyncio
async def test_write_no_tmp_leftover(writer: ResponseWriter, tmp_path: Path):
    source = tmp_path / "test.md"
    source.write_text("x")
    await writer.write_response(
        batch_id="batch123",
        source_files=[str(source)],
        evaluation_text="test",
        item_count=1,
    )
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == []


@pytest.mark.asyncio
async def test_write_valid_frontmatter(writer: ResponseWriter, tmp_path: Path):
    path = await writer.write_response(
        batch_id="batch123",
        source_files=["links.md", "notes.md"],
        evaluation_text="# Eval",
        item_count=2,
    )
    content = path.read_text()
    assert content.startswith("---\n")
    assert "batch_id: batch123" in content
    assert "links.md" in content


@pytest.mark.asyncio
async def test_write_unique_filenames(writer: ResponseWriter, tmp_path: Path):
    source = tmp_path / "same.md"
    source.write_text("x")
    p1 = await writer.write_response(
        batch_id="same1234",
        source_files=[str(source)],
        evaluation_text="first",
        item_count=1,
    )
    p2 = await writer.write_response(
        batch_id="same1234",
        source_files=[str(source)],
        evaluation_text="second",
        item_count=1,
    )
    assert p1 != p2
    assert p1.exists()
    assert p2.exists()
    assert p1.name == "same-1.genesis.md"
    assert p2.name == "same-2.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_skips_deleted_numbers(writer: ResponseWriter, tmp_path: Path):
    """Deleted file numbers are never reused — next write always increments."""
    source = tmp_path / "doc.md"
    source.write_text("x")
    # Write three responses: doc-1, doc-2, doc-3
    p1 = await writer.write_response(
        batch_id="m1", source_files=[str(source)],
        evaluation_text="first", item_count=1,
    )
    p2 = await writer.write_response(
        batch_id="m2", source_files=[str(source)],
        evaluation_text="second", item_count=1,
    )
    p3 = await writer.write_response(
        batch_id="m3", source_files=[str(source)],
        evaluation_text="third", item_count=1,
    )
    assert p1.name == "doc-1.genesis.md"
    assert p2.name == "doc-2.genesis.md"
    assert p3.name == "doc-3.genesis.md"
    # Delete the highest numbered file
    p3.unlink()
    # Next write should be doc-4, NOT doc-3 (the deleted number)
    p4 = await writer.write_response(
        batch_id="m4", source_files=[str(source)],
        evaluation_text="fourth", item_count=1,
    )
    assert p4.name == "doc-4.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_counter_survives_all_files_deleted(
    writer: ResponseWriter, tmp_path: Path,
):
    """Counter file preserves high-water mark even if all response files are deleted."""
    source = tmp_path / "note.md"
    source.write_text("x")
    # Write three responses
    await writer.write_response(
        batch_id="c1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    p2 = await writer.write_response(
        batch_id="c2", source_files=[str(source)],
        evaluation_text="b", item_count=1,
    )
    p3 = await writer.write_response(
        batch_id="c3", source_files=[str(source)],
        evaluation_text="c", item_count=1,
    )
    # Delete numbered response files
    p2.unlink()
    p3.unlink()
    # Next write must be note-4, not note-1 (counter persists)
    p4 = await writer.write_response(
        batch_id="c4", source_files=[str(source)],
        evaluation_text="d", item_count=1,
    )
    assert p4.name == "note-4.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_survives_counter_file_deletion(
    writer: ResponseWriter, tmp_path: Path,
):
    """If counter file is lost, falls back to highest number on disk."""
    source = tmp_path / "plan.md"
    source.write_text("x")
    # Write two responses (creates counter file)
    await writer.write_response(
        batch_id="f1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    await writer.write_response(
        batch_id="f2", source_files=[str(source)],
        evaluation_text="b", item_count=1,
    )
    # Delete counter file
    counter_file = tmp_path / ".genesis-counters.json"
    assert counter_file.exists()
    counter_file.unlink()
    # Falls back to disk scan: plan-1.genesis.md and plan-2.genesis.md exist
    # so next should be plan-3
    p3 = await writer.write_response(
        batch_id="f3", source_files=[str(source)],
        evaluation_text="c", item_count=1,
    )
    assert p3.name == "plan-3.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_ignores_non_numeric_suffixes(
    writer: ResponseWriter, tmp_path: Path,
):
    """Non-numeric suffixed files like base-draft.genesis.md don't affect numbering."""
    source = tmp_path / "ideas.md"
    source.write_text("x")
    # Create base + a non-numeric suffixed file
    (tmp_path / "ideas.genesis.md").write_text("base")
    (tmp_path / "ideas-draft.genesis.md").write_text("draft")
    (tmp_path / "ideas-2.genesis.md").write_text("two")
    p = await writer.write_response(
        batch_id="n1", source_files=[str(source)],
        evaluation_text="new", item_count=1,
    )
    # Should be ideas-3, ignoring the -draft file
    assert p.name == "ideas-3.genesis.md"


@pytest.mark.asyncio
async def test_write_preserves_content(writer: ResponseWriter, tmp_path: Path):
    source = tmp_path / "x.md"
    source.write_text("x")
    text = "## Detailed Analysis\n\nThis is a **thorough** evaluation."
    path = await writer.write_response(
        batch_id="batch999",
        source_files=[str(source)],
        evaluation_text=text,
        item_count=1,
    )
    content = path.read_text()
    assert text in content


@pytest.mark.asyncio
async def test_write_escapes_special_yaml_chars(writer: ResponseWriter, tmp_path: Path):
    """Frontmatter values with newlines, tabs, colons, quotes are properly escaped."""
    import yaml

    source = tmp_path / "special.md"
    source.write_text("x")
    path = await writer.write_response(
        batch_id="batch-with-\"quotes\"",
        source_files=[str(source) + "\nnewline\ttab: colon"],
        evaluation_text="body",
        item_count=1,
    )
    content = path.read_text()
    # Extract frontmatter between --- markers
    parts = content.split("---")
    assert len(parts) >= 3
    fm = yaml.safe_load(parts[1])
    # The dangerous characters should survive round-trip through yaml
    assert "\n" in fm["source_files"][0] or "newline" in fm["source_files"][0]
    assert fm["batch_id"] == 'batch-with-"quotes"'
