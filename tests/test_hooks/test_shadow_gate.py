"""Tests for the H-1 PR2a shadow novelty-gate projection (measurement-only).

PR2a simulates what the PR2b novelty gate (suppress already-surfaced IDs +
serendipity-boost never-surfaced memories) WOULD inject, and logs actual-vs-
projected per prompt — while the real injection output stays byte-identical.

The critical property is output-neutrality: ``_rrf_fusion(..., shadow={})`` must
return exactly what ``shadow=None`` returns. These tests also characterize the
(previously untested) real selection loop so PR2b's enforcement can't drift it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

# The hook lives in scripts/, not a package — load it manually (mirrors
# test_working_set.py). Unique module name to avoid sys.modules clashes.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_HOOK_PATH = _SCRIPTS_DIR / "proactive_memory_hook.py"
_spec = importlib.util.spec_from_file_location("proactive_memory_hook_shadow", _HOOK_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["proactive_memory_hook_shadow"] = _mod
_spec.loader.exec_module(_mod)

_NOW = "2026-07-09T12:00:00+00:00"


def _cm(mid: str, *, score_src="episodic", retrieved=5, content="real memory content") -> dict:
    """A content_map entry."""
    return {
        "memory_id": mid,
        "content": content,
        "collection": score_src,
        "_retrieved_count": retrieved,
    }


class TestShadowGateSuppression:
    def test_suppresses_repeat_and_promotes_novel(self) -> None:
        scores = {"a": 0.030, "b": 0.020, "c": 0.018, "d": 0.017}
        cmap = {m: _cm(m) for m in scores}
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset({"a"}))
        # 'a' suppressed → the below-top novel 'd' is promoted into the freed slot
        assert out["projected_ids"] == ["b", "c", "d"]
        assert "a" not in out["projected_ids"]
        assert out["suppressed"] == 1
        assert out["serendipity_boosted"] == 0

    def test_all_repeat_no_novel_projects_empty(self) -> None:
        scores = {"a": 0.030, "b": 0.020}
        cmap = {m: _cm(m) for m in scores}
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset({"a", "b"}))
        assert out["projected_ids"] == []
        assert out["projected_injected"] == 0
        assert out["suppressed"] == 2

    def test_all_top_repeat_but_novel_below_is_found(self) -> None:
        scores = {"a": 0.030, "b": 0.020, "c": 0.018}
        cmap = {m: _cm(m) for m in scores}
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset({"a", "b"}))
        assert out["projected_ids"] == ["c"]
        assert out["suppressed"] == 2

    def test_widened_scan_reaches_novel_beyond_real_bound(self) -> None:
        # 6 suppressed above-floor candidates (fill the real _MAX_RESULTS*2 scan),
        # then 3 novel ones only a *4 scan reaches.
        supp = {f"s{i}": 0.030 - i * 0.001 for i in range(6)}
        novel = {"g": 0.024, "h": 0.023, "i": 0.022}
        scores = {**supp, **novel}
        cmap = {m: _cm(m) for m in scores}
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset(supp))
        assert out["projected_ids"] == ["g", "h", "i"]
        assert out["suppressed"] == 6


class TestShadowGateSerendipity:
    def test_zero_retrieved_episodic_is_boosted_and_reorders(self) -> None:
        scores = {"a": 0.020, "b": 0.016}
        cmap = {"a": _cm("a", retrieved=5), "b": _cm("b", retrieved=0)}
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset())
        # b (0.016*1.3=0.0208) now outranks a (0.020)
        assert out["projected_ids"][0] == "b"
        assert out["serendipity_boosted"] == 1

    def test_knowledge_base_zero_retrieved_not_boosted(self) -> None:
        scores = {"a": 0.020, "b": 0.016}
        cmap = {"a": _cm("a", retrieved=5), "b": _cm("b", score_src="knowledge_base", retrieved=0)}
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset())
        assert out["serendipity_boosted"] == 0
        assert out["projected_ids"][0] == "a"  # order unchanged

    def test_unknown_retrieved_count_not_boosted(self) -> None:
        scores = {"a": 0.020, "b": 0.016}
        cmap = {"a": _cm("a", retrieved=5), "b": _cm("b", retrieved=-1)}  # FTS-only, unknown
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset())
        assert out["serendipity_boosted"] == 0
        assert out["projected_ids"][0] == "a"

    def test_boost_promotes_near_floor_but_not_far_below(self) -> None:
        floor = _mod._MIN_RRF_SCORE_RANK2  # 0.015
        near = floor / 1.3 + 0.0005   # boosted crosses floor
        far = 0.004                   # boosted (×1.3) still far below floor
        scores = {"a": 0.030, "near": near, "far": far}
        cmap = {
            "a": _cm("a", retrieved=5),
            "near": _cm("near", retrieved=0),
            "far": _cm("far", retrieved=0),
        }
        with patch.object(_mod, "_is_garbage", lambda c: False):
            out = _mod._shadow_gate(scores, cmap, frozenset())
        assert "near" in out["projected_ids"]
        assert "far" not in out["projected_ids"]


class TestOutputInvariance:
    """The real _rrf_fusion return must be identical with shadow on or off."""

    def _inputs(self):
        # Two FTS + one vector hit that also appears in FTS (score stacks),
        # plus a KB hit — exercises floor + the single-KB-slot rule.
        fts = [
            {"memory_id": "m1", "content": "alpha", "collection": "episodic"},
            {"memory_id": "m2", "content": "beta", "collection": "episodic"},
        ]
        vector = [
            {"memory_id": "m1", "content": "alpha", "collection": "episodic",
             "_retrieved_count": 3},
        ]
        knowledge = [
            {"memory_id": "k1", "content": "kb-fact", "collection": "knowledge_base",
             "confidence": 0.95, "_retrieved_count": 0},
        ]
        return fts, vector, knowledge

    def test_shadow_does_not_change_returned_results(self) -> None:
        fts, vector, knowledge = self._inputs()
        base = _mod._rrf_fusion(fts, vector, knowledge_results=knowledge, shadow=None)
        shadow: dict = {}
        withshadow = _mod._rrf_fusion(
            fts, vector, knowledge_results=knowledge,
            suppress_ids=frozenset({"m1"}), shadow=shadow,
        )
        assert [r["memory_id"] for r in withshadow] == [r["memory_id"] for r in base]
        assert withshadow == base  # full dict equality — nothing mutated
        # shadow populated (m1 suppressed → projection differs from actual)
        assert "projected_ids" in shadow
        assert "m1" not in shadow["projected_ids"]

    def test_real_selection_characterization(self) -> None:
        # Pin the real top-N so PR2b enforcement can't silently change it.
        fts, vector, knowledge = self._inputs()
        base = _mod._rrf_fusion(fts, vector, knowledge_results=knowledge, shadow=None)
        ids = [r["memory_id"] for r in base]
        assert ids[0] == "m1"  # stacked FTS+vector score wins
        assert "m2" in ids
        assert ids.count("k1") <= 1  # single KB slot honored


class TestComputeSuppressIds:
    def test_returns_working_set_entry_ids(self, tmp_path: Path) -> None:
        sid = "sess-a"
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            ws = _mod._load_working_set(sid)
            _mod._ws_record(ws, [("m1", "memory"), ("m2", "memory")], None, _NOW)
            _mod._save_working_set(sid, ws)
            with patch.object(_mod, "_WS_GATE_DISABLED_FLAG", tmp_path / "nope"):
                supp = _mod._compute_suppress_ids(sid)
        assert supp == frozenset({"m1", "m2"})

    def test_kill_switch_flag_disables_suppression(self, tmp_path: Path) -> None:
        sid = "sess-b"
        flag = tmp_path / "ws_gate_disabled"
        flag.write_text("")
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            ws = _mod._load_working_set(sid)
            _mod._ws_record(ws, [("m1", "memory")], None, _NOW)
            _mod._save_working_set(sid, ws)
            with patch.object(_mod, "_WS_GATE_DISABLED_FLAG", flag):
                supp = _mod._compute_suppress_ids(sid)
        assert supp == frozenset()

    def test_no_session_id_returns_empty(self, tmp_path: Path) -> None:
        with patch.object(_mod, "_WS_GATE_DISABLED_FLAG", tmp_path / "nope"):
            assert _mod._compute_suppress_ids("") == frozenset()


class TestWsMeasureShadowFields:
    def test_shadow_fields_merged_into_log_and_stats(self, tmp_path: Path) -> None:
        import json

        fused = [{"memory_id": "m1", "collection": "episodic"}]
        shadow = {
            "projected_ids": ["m9"],
            "projected_injected": 1,
            "suppressed": 1,
            "serendipity_boosted": 0,
        }
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            stats = _mod._ws_measure(fused, "s1", None, _NOW, shadow=shadow)
            log = (tmp_path / "s1" / _mod._WS_LOG_FILENAME).read_text().strip().splitlines()
        rec = json.loads(log[-1])
        assert rec["projected_injected"] == 1
        assert rec["suppressed"] == 1
        assert rec["serendipity_boosted"] == 0
        assert stats["projected_injected"] == 1
        assert stats["suppressed"] == 1

    def test_shadow_none_keeps_pr1_behavior(self, tmp_path: Path) -> None:
        import json

        fused = [{"memory_id": "m1", "collection": "episodic"}]
        with patch.object(_mod, "_TRAIL_DIR", tmp_path):
            stats = _mod._ws_measure(fused, "s1", None, _NOW)  # no shadow arg
            log = (tmp_path / "s1" / _mod._WS_LOG_FILENAME).read_text().strip().splitlines()
        rec = json.loads(log[-1])
        assert "projected_injected" not in rec  # PR1 log schema untouched
        assert "injected" in rec
        assert stats.get("projected_injected") is None or "projected_injected" not in stats
