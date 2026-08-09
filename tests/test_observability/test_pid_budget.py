"""Tests for the per-user systemd-slice PID-budget reader + status helper.

The PID/task budget (`user-<uid>.slice/pids.{current,max}`) is the resource that
actually maxes under many concurrent CC sessions (each spawns MCP subprocess
trees), yet nothing else in the infra snapshot tracks it — a `Cannot fork` can
happen while memory/cpu/disk all read green. This closes that blind spot.
"""

from __future__ import annotations

from genesis.observability.snapshots.infrastructure import (
    _collect_pid_budget,
    _pid_status,
)


class TestPidStatus:
    def test_none_is_healthy(self):
        # No sub-cap (pids.max == "max") → pct None → must never alarm.
        assert _pid_status(None) == "healthy"

    def test_thresholds(self):
        assert _pid_status(0.0) == "healthy"
        assert _pid_status(41.7) == "healthy"  # the normal box hum
        assert _pid_status(79.9) == "healthy"
        assert _pid_status(80.0) == "degraded"
        assert _pid_status(89.9) == "degraded"
        assert _pid_status(90.0) == "error"
        assert _pid_status(100.0) == "error"


class TestCollectPidBudget:
    def _write(self, base, current: str, maximum: str):
        (base / "pids.current").write_text(current)
        (base / "pids.max").write_text(maximum)

    def test_normal(self, tmp_path):
        self._write(tmp_path, "1000\n", "2400\n")
        out = _collect_pid_budget(str(tmp_path))
        assert out == {"status": "healthy", "current": 1000, "max": 2400, "pct": 41.7}

    def test_degraded(self, tmp_path):
        self._write(tmp_path, "2000\n", "2400\n")
        out = _collect_pid_budget(str(tmp_path))
        assert out["status"] == "degraded"
        assert out["pct"] == 83.3

    def test_error(self, tmp_path):
        self._write(tmp_path, "2300\n", "2400\n")
        out = _collect_pid_budget(str(tmp_path))
        assert out["status"] == "error"
        assert out["pct"] >= 90.0

    def test_max_sentinel_is_healthy_no_pct(self, tmp_path):
        # A slice with no sub-cap inherits the container root budget — not a
        # per-slice risk, so no pct and never an alarm.
        self._write(tmp_path, "1500\n", "max\n")
        out = _collect_pid_budget(str(tmp_path))
        assert out == {"status": "healthy", "current": 1500, "max": "max", "pct": None}

    def test_missing_files_unavailable(self, tmp_path):
        # Nothing written → unreadable → unavailable (never a false "healthy").
        out = _collect_pid_budget(str(tmp_path))
        assert out == {"status": "unavailable"}

    def test_zero_max_unavailable(self, tmp_path):
        # A 0 cap would divide-by-zero; must degrade to unavailable, not crash.
        self._write(tmp_path, "5\n", "0\n")
        out = _collect_pid_budget(str(tmp_path))
        assert out == {"status": "unavailable"}

    def test_malformed_current_unavailable(self, tmp_path):
        self._write(tmp_path, "notanint\n", "2400\n")
        out = _collect_pid_budget(str(tmp_path))
        assert out == {"status": "unavailable"}

    def test_shape_on_real_box(self):
        # Live call (no base_dir) must always return a dict with a valid status,
        # never raise — even on a host without the cgroup path.
        out = _collect_pid_budget()
        assert out["status"] in {"healthy", "degraded", "error", "unavailable"}
