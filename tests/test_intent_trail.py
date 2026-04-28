"""Tests for session intent trail — pivot detection and formatting."""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

# The hook script lives in scripts/, not a package — load it manually
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_HOOK_PATH = _SCRIPTS_DIR / "proactive_memory_hook.py"

# Load the module from file path
_spec = importlib.util.spec_from_file_location("proactive_memory_hook", _HOOK_PATH)
_mod = importlib.util.module_from_spec(_spec)
# Prevent the hook from auto-running or importing heavy deps at load time
sys.modules["proactive_memory_hook"] = _mod
_spec.loader.exec_module(_mod)

_jaccard_similarity = _mod._jaccard_similarity
_detect_pivot = _mod._detect_pivot
_update_and_format_trail = _mod._update_and_format_trail
_load_trail = _mod._load_trail
_save_trail = _mod._save_trail
_extract_keywords = _mod._extract_keywords
_PIVOT_SIMILARITY_THRESHOLD = _mod._PIVOT_SIMILARITY_THRESHOLD
_PIVOT_DEBOUNCE_MSGS = _mod._PIVOT_DEBOUNCE_MSGS


class TestJaccardSimilarity:
    def test_identical(self) -> None:
        assert _jaccard_similarity(["a", "b", "c"], ["a", "b", "c"]) == 1.0

    def test_disjoint(self) -> None:
        assert _jaccard_similarity(["a", "b"], ["c", "d"]) == 0.0

    def test_partial_overlap(self) -> None:
        sim = _jaccard_similarity(["a", "b", "c"], ["a", "d", "e"])
        assert 0.1 < sim < 0.3  # 1/5 = 0.2

    def test_empty_first(self) -> None:
        assert _jaccard_similarity([], ["a", "b"]) == 0.0

    def test_empty_second(self) -> None:
        assert _jaccard_similarity(["a", "b"], []) == 0.0

    def test_both_empty(self) -> None:
        assert _jaccard_similarity([], []) == 0.0


class TestDetectPivot:
    def test_first_message_is_always_pivot(self) -> None:
        trail = {"pivots": [], "last_keywords": [], "msg_count": 0}
        assert _detect_pivot(["executor", "pipeline"], trail) is True

    def test_similar_keywords_no_pivot(self) -> None:
        trail = {
            "pivots": [{"idx": 0, "at_msg": 1}],
            "last_keywords": ["executor", "pipeline", "test"],
            "msg_count": 10,
        }
        assert _detect_pivot(["executor", "pipeline", "run"], trail) is False

    def test_different_keywords_triggers_pivot(self) -> None:
        trail = {
            "pivots": [{"idx": 0, "at_msg": 1}],
            "last_keywords": ["executor", "pipeline", "test"],
            "msg_count": 10,
        }
        assert _detect_pivot(["memory", "recall", "search"], trail) is True

    def test_debounce_prevents_rapid_pivots(self) -> None:
        trail = {
            "pivots": [{"idx": 0, "at_msg": 8}],
            "last_keywords": ["executor", "pipeline"],
            "msg_count": 9,  # Only 1 message since last pivot
        }
        assert _detect_pivot(["memory", "recall", "search"], trail) is False

    def test_debounce_allows_after_threshold(self) -> None:
        trail = {
            "pivots": [{"idx": 0, "at_msg": 5}],
            "last_keywords": ["executor", "pipeline"],
            "msg_count": 9,  # 4 messages since last pivot (> debounce of 3)
        }
        assert _detect_pivot(["memory", "recall", "search"], trail) is True

    def test_empty_keywords_no_pivot(self) -> None:
        trail = {"pivots": [], "last_keywords": ["something"], "msg_count": 5}
        assert _detect_pivot([], trail) is False


class TestTrailIO:
    def test_load_missing_file(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            trail = _load_trail("nonexistent-session")
            assert trail["pivots"] == []
            assert trail["msg_count"] == 0

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            trail = {
                "session_id": "test-123",
                "pivots": [{"idx": 0, "label": "test topic", "ts": "2026-01-01"}],
                "last_keywords": ["test", "topic"],
                "msg_count": 5,
            }
            _save_trail("test-123", trail)
            loaded = _load_trail("test-123")
            assert loaded["pivots"] == trail["pivots"]
            assert loaded["msg_count"] == 5


class TestUpdateAndFormatTrail:
    def test_returns_none_under_2_pivots(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path), \
             patch.object(_mod, "_DB_PATH", tmp_path / "fake.db"):
            result = _update_and_format_trail("s1", ["executor", "pipeline"], "test prompt")
            # First call creates pivot 0 — but only 1 pivot, so None
            assert result is None

    def test_returns_trail_with_2_pivots(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path), \
             patch.object(_mod, "_DB_PATH", tmp_path / "fake.db"):
            # First pivot
            _update_and_format_trail("s1", ["executor", "pipeline"], "fix the executor")
            # Simulate enough messages for debounce
            trail = _load_trail("s1")
            trail["msg_count"] = 10
            _save_trail("s1", trail)
            # Second pivot with different keywords
            result = _update_and_format_trail("s1", ["memory", "recall", "search"], "search memory")
            assert result is not None
            assert "[Session trail]" in result
            assert "→" in result

    def test_no_session_id_returns_none(self) -> None:
        result = _update_and_format_trail("", ["test"], "test")
        assert result is None

    def test_trail_format_arrow_separated(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path), \
             patch.object(_mod, "_DB_PATH", tmp_path / "fake.db"):
            # Manually build a trail with 3 pivots
            trail = {
                "session_id": "s2",
                "pivots": [
                    {"idx": 0, "label": "topic one", "ts": "", "at_msg": 1},
                    {"idx": 1, "label": "topic two", "ts": "", "at_msg": 5},
                    {"idx": 2, "label": "topic three", "ts": "", "at_msg": 10},
                ],
                "last_keywords": ["topic", "three"],
                "msg_count": 15,
            }
            _save_trail("s2", trail)
            # Call with same keywords — no new pivot, just format
            result = _update_and_format_trail("s2", ["topic", "three"], "topic three stuff")
            assert result == "[Session trail] topic one → topic two → topic three"

    def test_long_trail_truncated(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path), \
             patch.object(_mod, "_DB_PATH", tmp_path / "fake.db"):
            pivots = [
                {"idx": i, "label": f"topic {i}", "ts": "", "at_msg": i * 5}
                for i in range(12)
            ]
            trail = {
                "session_id": "s3",
                "pivots": pivots,
                "last_keywords": ["topic", "eleven"],
                "msg_count": 60,
            }
            _save_trail("s3", trail)
            # Use same keywords to avoid triggering a new pivot
            result = _update_and_format_trail("s3", ["topic", "eleven"], "topic eleven")
            assert result is not None
            assert result.startswith("[Session trail] … → ")
            # Should show last 8 (indices 4-11)
            assert "topic 11" in result
            assert "topic 4" in result
            assert "topic 3" not in result


class TestObservationStorage:
    def test_pivot_observation_written(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """CREATE TABLE observations (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                priority TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.commit()
        conn.close()

        _mod._record_pivot_observation(db_path, "test-session", "memory search", "let's search memory")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT * FROM observations").fetchone()
        conn.close()

        assert row is not None
        assert "session:test-session" in row[1]  # source
        assert row[2] == "conversation_pivot"  # type
        assert "memory search" in row[3]  # content
