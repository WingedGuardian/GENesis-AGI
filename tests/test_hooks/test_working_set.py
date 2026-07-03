"""Tests for the per-session working set — H-1 PR1 injection-overlap measurement.

The working set records which memory/KB/code IDs the proactive hook has
already injected into a session (``~/.genesis/sessions/{sid}/surfaced_memories.json``)
plus an append-only ``injection_log.jsonl`` used by the observability
snapshot to compute 7-day overlap. PR1 is record-only: injection output
must stay byte-identical.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

# The hook script lives in scripts/, not a package — load it manually
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_HOOK_PATH = _SCRIPTS_DIR / "proactive_memory_hook.py"

_spec = importlib.util.spec_from_file_location("proactive_memory_hook_ws", _HOOK_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["proactive_memory_hook_ws"] = _mod
_spec.loader.exec_module(_mod)

_NOW = "2026-07-03T12:00:00+00:00"
_LATER = "2026-07-03T13:00:00+00:00"


def _empty_ws(session_id: str = "s1") -> dict:
    return _mod._load_working_set(session_id)


class TestWorkingSetIO:
    def test_load_missing_returns_empty_structure(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            ws = _mod._load_working_set("nonexistent-session")
        assert ws["version"] == _mod._WS_VERSION
        assert ws["session_id"] == "nonexistent-session"
        assert ws["turn"] == 0
        assert ws["entries"] == {}
        assert ws["procedures"] == {}
        assert ws["resets"] == []

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            ws = _mod._load_working_set("s1")
            _mod._ws_record(ws, [("mem-a", "memory"), ("kb-b", "kb")], "proc-1", _NOW)
            _mod._save_working_set("s1", ws)
            loaded = _mod._load_working_set("s1")
        assert loaded["turn"] == 1
        assert loaded["entries"]["mem-a"]["kind"] == "memory"
        assert loaded["entries"]["kb-b"]["kind"] == "kb"
        assert loaded["procedures"]["proc-1"]["count"] == 1

    def test_corrupted_file_returns_empty(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "s1"
        session_dir.mkdir(parents=True)
        (session_dir / "surfaced_memories.json").write_text("{not json!!")
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            ws = _mod._load_working_set("s1")
        assert ws["entries"] == {}
        assert ws["turn"] == 0

    def test_non_dict_payload_returns_empty(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "s1"
        session_dir.mkdir(parents=True)
        (session_dir / "surfaced_memories.json").write_text('["a", "list"]')
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            ws = _mod._load_working_set("s1")
        assert ws["entries"] == {}

    def test_path_traversal_session_id_is_noop(self, tmp_path: Path) -> None:
        """Session IDs come from hook stdin — never build paths from ../ or /."""
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            assert _mod._ws_path("../evil") is None
            assert _mod._ws_path("a/b") is None
            assert _mod._ws_path("") is None
            ws = _mod._load_working_set("../evil")
            assert ws["entries"] == {}
            # Save must be a silent no-op, not a crash or a file outside tmp
            _mod._save_working_set("../evil", ws)
        assert list(tmp_path.iterdir()) == []


class TestWsOverlap:
    def test_overlap_math(self) -> None:
        ws = _empty_ws()
        _mod._ws_record(ws, [("a", "memory"), ("b", "memory")], None, _NOW)
        repeats, pct = _mod._ws_overlap(ws, ["a", "c"])
        assert repeats == ["a"]
        assert pct == 50.0

    def test_no_injected_ids_zero_pct(self) -> None:
        ws = _empty_ws()
        _mod._ws_record(ws, [("a", "memory")], None, _NOW)
        repeats, pct = _mod._ws_overlap(ws, [])
        assert repeats == []
        assert pct == 0.0

    def test_empty_working_set_zero_overlap(self) -> None:
        repeats, pct = _mod._ws_overlap(_empty_ws(), ["a", "b"])
        assert repeats == []
        assert pct == 0.0

    def test_all_repeats(self) -> None:
        ws = _empty_ws()
        _mod._ws_record(ws, [("a", "memory"), ("b", "kb")], None, _NOW)
        repeats, pct = _mod._ws_overlap(ws, ["a", "b"])
        assert sorted(repeats) == ["a", "b"]
        assert pct == 100.0


class TestWsRecord:
    def test_upsert_bumps_counts_and_turns(self) -> None:
        ws = _empty_ws()
        _mod._ws_record(ws, [("a", "memory")], None, _NOW)
        assert ws["turn"] == 1
        entry = ws["entries"]["a"]
        assert entry["count"] == 1
        assert entry["first_turn"] == 1
        assert entry["last_turn"] == 1
        assert entry["first_ts"] == _NOW

        _mod._ws_record(ws, [("a", "memory"), ("b", "code")], None, _LATER)
        assert ws["turn"] == 2
        entry = ws["entries"]["a"]
        assert entry["count"] == 2
        assert entry["first_turn"] == 1
        assert entry["last_turn"] == 2
        assert entry["first_ts"] == _NOW
        assert entry["last_ts"] == _LATER
        assert ws["entries"]["b"]["kind"] == "code"

    def test_procedure_tracking(self) -> None:
        ws = _empty_ws()
        _mod._ws_record(ws, [], "proc-1", _NOW)
        assert ws["procedures"]["proc-1"]["count"] == 1
        _mod._ws_record(ws, [], "proc-1", _LATER)
        assert ws["procedures"]["proc-1"]["count"] == 2
        assert ws["procedures"]["proc-1"]["last_ts"] == _LATER

    def test_eviction_keeps_newest_at_cap(self) -> None:
        ws = _empty_ws()
        # Fill to cap with increasing timestamps, oldest first
        for i in range(_mod._WS_MAX_ENTRIES):
            _mod._ws_record(
                ws, [(f"m{i}", "memory")], None, f"2026-07-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
            )
        assert len(ws["entries"]) == _mod._WS_MAX_ENTRIES
        # One more evicts the oldest (m0), keeps the newest
        _mod._ws_record(ws, [("overflow", "memory")], None, "2026-07-02T00:00:00+00:00")
        assert len(ws["entries"]) == _mod._WS_MAX_ENTRIES
        assert "overflow" in ws["entries"]
        assert "m0" not in ws["entries"]
        assert "m1" in ws["entries"]


class TestWsKind:
    def test_kind_classification(self) -> None:
        assert _mod._ws_kind({"memory_id": "abc", "collection": "episodic_memory"}) == "memory"
        assert _mod._ws_kind({"memory_id": "kbx", "collection": "knowledge_base"}) == "kb"
        assert _mod._ws_kind({"memory_id": "code:src/mod.py:fn"}) == "code"


class TestInjectionLog:
    def test_appends_jsonl_lines(self, tmp_path: Path) -> None:
        record1 = {"ts": _NOW, "turn": 1, "injected": 3, "repeats": 0,
                   "overlap_pct": 0.0, "ws_size": 3}
        record2 = {"ts": _LATER, "turn": 2, "injected": 2, "repeats": 1,
                   "overlap_pct": 50.0, "ws_size": 4}
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            _mod._append_injection_log("s1", record1)
            _mod._append_injection_log("s1", record2)
        log_path = tmp_path / "s1" / "injection_log.jsonl"
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["injected"] == 3
        assert json.loads(lines[1])["repeats"] == 1

    def test_bad_session_id_is_noop(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            _mod._append_injection_log("../evil", {"ts": _NOW})
        assert list(tmp_path.iterdir()) == []


class TestRecordDetailOverlapFields:
    def test_new_fields_present(self, tmp_path: Path) -> None:
        metrics_path = tmp_path / "proactive_metrics.json"
        with patch.object(_mod, "_METRICS_PATH", metrics_path):
            _mod._record_detail(
                fts_count=2,
                vector_count=3,
                fused_count=3,
                embed_latency_ms=450.0,
                total_latency_ms=800.0,
                fts_only_fallback=False,
                heartbeat_ms=5.0,
                injected_ids=["a", "b", "c"],
                repeat_count=1,
                overlap_pct=33.3,
                working_set_size=12,
                zero_retrieved_injected=2,
                procedure_repeat=True,
            )
        data = json.loads(metrics_path.read_text())
        assert data["injected_ids"] == ["a", "b", "c"]
        assert data["repeat_count"] == 1
        assert data["overlap_pct"] == 33.3
        assert data["working_set_size"] == 12
        assert data["zero_retrieved_injected"] == 2
        assert data["procedure_repeat"] is True

    def test_defaults_when_no_injection(self, tmp_path: Path) -> None:
        """Existing call shape (no new kwargs) still works — fields default."""
        metrics_path = tmp_path / "proactive_metrics.json"
        with patch.object(_mod, "_METRICS_PATH", metrics_path):
            _mod._record_detail(
                fts_count=0,
                vector_count=0,
                fused_count=0,
                embed_latency_ms=None,
                total_latency_ms=100.0,
                fts_only_fallback=False,
            )
        data = json.loads(metrics_path.read_text())
        assert data["injected_ids"] == []
        assert data["repeat_count"] == 0
        assert data["overlap_pct"] is None
        assert data["working_set_size"] is None
        assert data["zero_retrieved_injected"] == 0
        assert data["procedure_repeat"] is False
