"""Tests for _strip_gitnexus_block — keeps GitNexus's block out of CLAUDE.md."""

from __future__ import annotations

from pathlib import Path

from genesis.surplus.scheduler import _strip_gitnexus_block

_BLOCK = (
    "<!-- gitnexus:start -->\n"
    "# GitNexus — Code Intelligence\n\n"
    "This project is indexed by GitNexus as **GENesis-AGI** (43479 symbols).\n"
    "- MUST run impact analysis before editing any symbol.\n"
    "<!-- gitnexus:end -->\n"
)


def test_strips_block_preserving_surrounding_content(tmp_path: Path) -> None:
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Project Instructions\n\nReal content here.\n\n" + _BLOCK)
    assert _strip_gitnexus_block(f) is True
    out = f.read_text()
    assert "gitnexus:start" not in out and "gitnexus:end" not in out
    assert "GitNexus — Code Intelligence" not in out
    assert "# Project Instructions" in out
    assert "Real content here." in out
    assert out.endswith("\n")


def test_block_mid_file_only_removes_block(tmp_path: Path) -> None:
    f = tmp_path / "CLAUDE.md"
    f.write_text("top content\n\n" + _BLOCK + "\nbottom content\n")
    assert _strip_gitnexus_block(f) is True
    out = f.read_text()
    assert "gitnexus" not in out
    assert "top content" in out and "bottom content" in out


def test_no_block_returns_false_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "CLAUDE.md"
    original = "# Just instructions\n\nNo gitnexus here.\n"
    f.write_text(original)
    assert _strip_gitnexus_block(f) is False
    assert f.read_text() == original


def test_idempotent(tmp_path: Path) -> None:
    f = tmp_path / "CLAUDE.md"
    f.write_text("head\n\n" + _BLOCK)
    assert _strip_gitnexus_block(f) is True
    once = f.read_text()
    assert _strip_gitnexus_block(f) is False  # nothing left to strip
    assert f.read_text() == once


def test_missing_file_returns_false(tmp_path: Path) -> None:
    assert _strip_gitnexus_block(tmp_path / "nope.md") is False
