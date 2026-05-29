"""Regression tests for proactive_memory_hook keyword extraction.

The ``_keywords_from_files`` function strips the project root prefix from
absolute file paths to extract meaningful keywords.  A bug (literal ``${HOME}``
replacement) meant the prefix was never stripped, leaking path components
like 'home' and 'ubuntu' into search keywords.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Hook lives outside the package tree — add scripts/ to import path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from proactive_memory_hook import _keywords_from_files  # noqa: E402


class TestKeywordExtraction:

    def test_absolute_path_strips_project_prefix(self):
        keywords = _keywords_from_files([
            f"{Path.home()}/genesis/src/genesis/memory/store.py",
        ])
        assert "memory" in keywords
        assert "store" in keywords
        # Path components from the home directory should NOT appear
        assert "home" not in keywords
        assert "ubuntu" not in keywords

    def test_multiple_files(self):
        keywords = _keywords_from_files([
            f"{Path.home()}/genesis/src/genesis/memory/store.py",
            f"{Path.home()}/genesis/scripts/file_context_hook.py",
        ])
        assert "memory" in keywords
        assert "store" in keywords
        assert "file" in keywords
        assert "context" in keywords

    def test_already_relative_path(self):
        """If a path is somehow already relative, keywords still extracted."""
        keywords = _keywords_from_files(["src/genesis/routing/config.py"])
        assert "routing" in keywords
        assert "config" in keywords

    def test_empty_list(self):
        assert _keywords_from_files([]) == []

    def test_deduplication(self):
        """Same keyword from multiple files appears only once."""
        keywords = _keywords_from_files([
            f"{Path.home()}/genesis/src/genesis/memory/store.py",
            f"{Path.home()}/genesis/src/genesis/memory/recall.py",
        ])
        assert keywords.count("memory") == 1
