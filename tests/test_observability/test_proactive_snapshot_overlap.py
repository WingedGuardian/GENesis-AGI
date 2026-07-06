"""Tests for the 7-day injection-overlap aggregation — H-1 PR1.

``_overlap_7d`` globs ``~/.genesis/sessions/*/injection_log.jsonl`` (written
by the proactive memory hook) and aggregates overlap stats into the
``proactive_memory`` health section via ``proactive_memory_metrics``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from genesis.observability.snapshots import proactive_memory as pm

_NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)


def _write_log(sessions_dir: Path, session_id: str, records: list[dict]) -> Path:
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "injection_log.jsonl"
    with open(path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def _rec(ts: datetime, injected: int, repeats: int) -> dict:
    return {
        "ts": ts.isoformat(),
        "turn": 1,
        "injected": injected,
        "repeats": repeats,
        "overlap_pct": (100.0 * repeats / injected) if injected else 0.0,
        "ws_size": injected,
    }


class TestOverlap7d:
    def test_aggregates_across_sessions(self, tmp_path: Path) -> None:
        recent = _NOW - timedelta(days=1)
        _write_log(tmp_path, "s1", [_rec(recent, 3, 0), _rec(recent, 2, 1)])
        _write_log(tmp_path, "s2", [_rec(recent, 5, 4)])
        result = pm._overlap_7d(sessions_dir=tmp_path, now=_NOW)
        # totals: injected=10, repeats=5 → 50.0%
        assert result["overlap_pct_7d"] == 50.0
        assert result["prompts_with_injection_7d"] == 3
        # 2 of 3 prompts had ≥1 repeat
        assert result["repeat_prompt_rate_7d"] == round(2 / 3, 3)
        assert result["sessions_7d"] == 2

    def test_skips_lines_older_than_7d(self, tmp_path: Path) -> None:
        old = _NOW - timedelta(days=10)
        recent = _NOW - timedelta(days=2)
        _write_log(tmp_path, "s1", [_rec(old, 100, 100), _rec(recent, 4, 1)])
        result = pm._overlap_7d(sessions_dir=tmp_path, now=_NOW)
        assert result["overlap_pct_7d"] == 25.0
        assert result["prompts_with_injection_7d"] == 1

    def test_skips_stale_files_by_mtime(self, tmp_path: Path) -> None:
        recent = _NOW - timedelta(days=1)
        stale = _write_log(tmp_path, "s-old", [_rec(recent, 9, 9)])
        # Backdate the file itself past the window — prefilter must skip it
        old_epoch = (_NOW - timedelta(days=30)).timestamp()
        os.utime(stale, (old_epoch, old_epoch))
        _write_log(tmp_path, "s-new", [_rec(recent, 4, 1)])
        result = pm._overlap_7d(sessions_dir=tmp_path, now=_NOW)
        assert result["prompts_with_injection_7d"] == 1
        assert result["overlap_pct_7d"] == 25.0

    def test_skips_garbled_lines(self, tmp_path: Path) -> None:
        recent = _NOW - timedelta(days=1)
        path = _write_log(tmp_path, "s1", [_rec(recent, 4, 2)])
        with open(path, "a") as f:
            f.write("{corrupt line\n")
            f.write('{"ts": "not-a-timestamp", "injected": 5, "repeats": 5}\n')
        result = pm._overlap_7d(sessions_dir=tmp_path, now=_NOW)
        assert result["prompts_with_injection_7d"] == 1
        assert result["overlap_pct_7d"] == 50.0

    def test_zero_injection_lines_excluded_from_prompt_count(self, tmp_path: Path) -> None:
        recent = _NOW - timedelta(days=1)
        _write_log(tmp_path, "s1", [_rec(recent, 0, 0), _rec(recent, 2, 0)])
        result = pm._overlap_7d(sessions_dir=tmp_path, now=_NOW)
        assert result["prompts_with_injection_7d"] == 1
        assert result["overlap_pct_7d"] == 0.0
        assert result["repeat_prompt_rate_7d"] == 0.0

    def test_invalid_utf8_does_not_abort_aggregation(self, tmp_path: Path) -> None:
        """A log file with invalid UTF-8 bytes must not kill the whole rollup.

        Pre-hardening, read_text() raised UnicodeDecodeError (a ValueError,
        NOT caught by the per-file OSError guard) which escaped to the outer
        catch and silently dropped every remaining session. Valid lines in
        the corrupt file itself must still be salvaged.
        """
        recent = _NOW - timedelta(days=1)
        bad_dir = tmp_path / "s-corrupt"
        bad_dir.mkdir(parents=True)
        with open(bad_dir / "injection_log.jsonl", "wb") as f:
            f.write(b"\xff\xfe corrupt bytes\n")
            f.write((json.dumps(_rec(recent, 2, 1)) + "\n").encode())
        _write_log(tmp_path, "s-good", [_rec(recent, 4, 1)])
        result = pm._overlap_7d(sessions_dir=tmp_path, now=_NOW)
        # Both the salvaged line AND the other session must count
        assert result["prompts_with_injection_7d"] == 2
        assert result["sessions_7d"] == 2
        assert result["overlap_pct_7d"] == round(100.0 * 2 / 6, 1)

    def test_empty_dir_returns_zeros(self, tmp_path: Path) -> None:
        result = pm._overlap_7d(sessions_dir=tmp_path, now=_NOW)
        assert result["overlap_pct_7d"] == 0.0
        assert result["prompts_with_injection_7d"] == 0
        assert result["repeat_prompt_rate_7d"] == 0.0
        assert result["sessions_7d"] == 0

    def test_missing_dir_returns_zeros(self, tmp_path: Path) -> None:
        result = pm._overlap_7d(sessions_dir=tmp_path / "nope", now=_NOW)
        assert result["prompts_with_injection_7d"] == 0


class TestMetricsIncludesOverlap:
    def test_overlap_key_present(self, tmp_path: Path) -> None:
        recent = _NOW - timedelta(days=1)
        _write_log(tmp_path, "s1", [_rec(recent, 2, 1)])
        metrics_path = tmp_path / "proactive_metrics.json"
        metrics_path.write_text(json.dumps({"fused_results": 2}))
        with patch.object(pm, "_SESSIONS_DIR", tmp_path), \
             patch.object(pm, "_METRICS_PATH", metrics_path), \
             patch.object(pm, "_utcnow", lambda: _NOW):
            data = pm.proactive_memory_metrics()
        assert data["fused_results"] == 2
        assert data["overlap_7d"]["overlap_pct_7d"] == 50.0

    def test_overlap_key_present_even_without_metrics_file(self, tmp_path: Path) -> None:
        with patch.object(pm, "_SESSIONS_DIR", tmp_path), \
             patch.object(pm, "_METRICS_PATH", tmp_path / "missing.json"), \
             patch.object(pm, "_utcnow", lambda: _NOW):
            data = pm.proactive_memory_metrics()
        assert data["overlap_7d"]["prompts_with_injection_7d"] == 0
