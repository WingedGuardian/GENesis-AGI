"""Tests for inbox scanner — filesystem scanning and change detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.inbox.scanner import compute_hash, detect_changes, read_content, scan_folder


@pytest.fixture
def inbox_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def test_scan_folder_finds_files(inbox_dir: Path):
    (inbox_dir / "links.md").write_text("some links")
    (inbox_dir / "notes.txt").write_text("a note")
    result = scan_folder(inbox_dir)
    assert len(result) == 2
    names = {p.name for p in result}
    assert names == {"links.md", "notes.txt"}


def test_scan_folder_excludes_response_dir(inbox_dir: Path):
    (inbox_dir / "file.md").write_text("content")
    (inbox_dir / "_genesis").mkdir()
    (inbox_dir / "_genesis" / "response.md").write_text("response")
    result = scan_folder(inbox_dir, "_genesis")
    assert len(result) == 1
    assert result[0].name == "file.md"


def test_scan_folder_excludes_hidden(inbox_dir: Path):
    (inbox_dir / ".hidden").write_text("secret")
    (inbox_dir / "visible.md").write_text("ok")
    result = scan_folder(inbox_dir)
    assert len(result) == 1
    assert result[0].name == "visible.md"


def test_scan_folder_excludes_underscore_prefixed(inbox_dir: Path):
    (inbox_dir / "_draft.md").write_text("draft")
    (inbox_dir / "final.md").write_text("done")
    result = scan_folder(inbox_dir)
    assert len(result) == 1
    assert result[0].name == "final.md"


def test_scan_folder_excludes_response_suffix(inbox_dir: Path):
    (inbox_dir / "links.md").write_text("some links")
    (inbox_dir / "links.genesis.md").write_text("response")
    result = scan_folder(inbox_dir)
    assert len(result) == 1
    assert result[0].name == "links.md"


def test_scan_folder_empty(inbox_dir: Path):
    result = scan_folder(inbox_dir)
    assert result == []


def test_scan_folder_missing_dir(tmp_path: Path):
    result = scan_folder(tmp_path / "nonexistent")
    assert result == []


def test_compute_hash_deterministic(inbox_dir: Path):
    f = inbox_dir / "test.md"
    f.write_text("hello world")
    h1 = compute_hash(f)
    h2 = compute_hash(f)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_compute_hash_changes_on_modify(inbox_dir: Path):
    f = inbox_dir / "test.md"
    f.write_text("version 1")
    h1 = compute_hash(f)
    f.write_text("version 2")
    h2 = compute_hash(f)
    assert h1 != h2


def test_read_content_utf8(inbox_dir: Path):
    f = inbox_dir / "test.md"
    f.write_text("hello 世界", encoding="utf-8")
    content = read_content(f)
    assert content == "hello 世界"


def test_read_content_truncation(inbox_dir: Path):
    f = inbox_dir / "big.txt"
    f.write_text("x" * 100)
    content = read_content(f, max_bytes=10)
    assert len(content) == 10


def test_read_content_binary_graceful(inbox_dir: Path):
    f = inbox_dir / "binary.bin"
    f.write_bytes(b"\x80\x81\x82\x83")
    content = read_content(f)
    assert isinstance(content, str)


def test_detect_changes_new_files(inbox_dir: Path):
    (inbox_dir / "new.md").write_text("brand new")
    new, modified = detect_changes(inbox_dir, {})
    assert len(new) == 1
    assert new[0].name == "new.md"
    assert modified == []


def test_detect_changes_modified_files(inbox_dir: Path):
    f = inbox_dir / "existing.md"
    f.write_text("original")
    known = {str(f): compute_hash(f)}
    f.write_text("modified content")
    new, modified = detect_changes(inbox_dir, known)
    assert new == []
    assert len(modified) == 1
    assert modified[0].name == "existing.md"


def test_detect_changes_unchanged(inbox_dir: Path):
    f = inbox_dir / "stable.md"
    f.write_text("no change")
    known = {str(f): compute_hash(f)}
    new, modified = detect_changes(inbox_dir, known)
    assert new == []
    assert modified == []


def test_detect_changes_deleted_file(inbox_dir: Path):
    """Deleted files just don't appear — no error, no result."""
    known = {"/some/deleted/file.md": "abc123"}
    new, modified = detect_changes(inbox_dir, known)
    assert new == []
    assert modified == []


# ── Recursive scanning ────────────────────────────────────────────────


def test_scan_folder_recursive_finds_subdirectories(inbox_dir: Path):
    """Recursive mode finds files in subdirectories."""
    (inbox_dir / "top.md").write_text("top level")
    sub = inbox_dir / "subdir"
    sub.mkdir()
    (sub / "deep.md").write_text("nested")
    result = scan_folder(inbox_dir, recursive=True)
    names = {p.name for p in result}
    assert names == {"top.md", "deep.md"}


def test_scan_folder_non_recursive_ignores_subdirectories(inbox_dir: Path):
    """Non-recursive mode (default) ignores subdirectory contents."""
    (inbox_dir / "top.md").write_text("top level")
    sub = inbox_dir / "subdir"
    sub.mkdir()
    (sub / "deep.md").write_text("nested")
    result = scan_folder(inbox_dir, recursive=False)
    names = {p.name for p in result}
    assert names == {"top.md"}


def test_scan_folder_recursive_excludes_hidden_dirs(inbox_dir: Path):
    """Recursive mode skips files inside hidden directories."""
    hidden = inbox_dir / ".obsidian"
    hidden.mkdir()
    (hidden / "config.json").write_text("{}")
    (inbox_dir / "visible.md").write_text("ok")
    result = scan_folder(inbox_dir, recursive=True)
    names = {p.name for p in result}
    assert names == {"visible.md"}


def test_scan_folder_recursive_excludes_response_dir(inbox_dir: Path):
    """Recursive mode skips files inside the response_dir."""
    gen = inbox_dir / "_genesis"
    gen.mkdir()
    (gen / "response.md").write_text("response")
    (inbox_dir / "source.md").write_text("content")
    result = scan_folder(inbox_dir, "_genesis", recursive=True)
    names = {p.name for p in result}
    assert names == {"source.md"}


def test_detect_changes_recursive(inbox_dir: Path):
    """detect_changes with recursive=True finds nested files."""
    sub = inbox_dir / "subdir"
    sub.mkdir()
    (sub / "nested.md").write_text("nested content")
    new, modified = detect_changes(inbox_dir, {}, recursive=True)
    assert len(new) == 1
    assert new[0].name == "nested.md"
