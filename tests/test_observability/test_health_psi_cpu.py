"""Tests for the PSI reader + CPU/memory health-status helpers.

Covers PR-2b′ (health-surface honesty): CPU status derived from plain
utilization % (not loadavg), and PSI (pressure stall information) surfaced as
the honest "is contention actually hurting" signal, folded into memory status.
"""

from __future__ import annotations

import sys

from genesis.observability.snapshots.infrastructure import (
    _collect_cpu_usage,
    _cpu_status,
    _read_psi,
    _worse_status,
    memory_status,
)

# The `snapshots` package re-exports the `infrastructure` *function*, shadowing
# the submodule name — grab the real module object for monkeypatching globals.
infra = sys.modules["genesis.observability.snapshots.infrastructure"]

_PSI_SAMPLE = (
    "some avg10=6.72 avg60=8.89 avg300=10.68 total=123456\n"
    "full avg10=0.95 avg60=1.10 avg300=1.22 total=65432\n"
)


class TestReadPsi:
    def test_parses_some_and_full(self, tmp_path):
        p = tmp_path / "cpu.pressure"
        p.write_text(_PSI_SAMPLE)
        out = _read_psi(str(p))
        assert out["some_avg60"] == 8.89
        assert out["full_avg10"] == 0.95
        assert out["full_avg300"] == 1.22
        # only avg10/60/300 kept — 'total' is dropped
        assert not any(k.endswith("total") for k in out)
        assert len(out) == 6

    def test_missing_file_returns_empty(self):
        assert _read_psi("/sys/fs/cgroup/definitely-not-a-real.pressure") == {}

    def test_malformed_lines_are_skipped(self, tmp_path):
        p = tmp_path / "bad.pressure"
        p.write_text("garbage line\nsome avg10=notafloat avg60=2.0 total=5\n\n")
        out = _read_psi(str(p))
        # avg10 unparseable → skipped; avg60 parsed
        assert out == {"some_avg60": 2.0}


class TestCpuStatus:
    def test_none_is_healthy(self):
        # baseline / no-data must never alarm
        assert _cpu_status(None) == "healthy"

    def test_thresholds(self):
        assert _cpu_status(0.0) == "healthy"
        assert _cpu_status(45.0) == "healthy"  # the normal box hum
        assert _cpu_status(79.9) == "healthy"
        assert _cpu_status(80.0) == "degraded"
        assert _cpu_status(94.9) == "degraded"
        assert _cpu_status(95.0) == "error"
        assert _cpu_status(100.0) == "error"


class TestMemoryStatus:
    def test_anon_only(self):
        assert memory_status(0.36, {}) == "healthy"
        assert memory_status(0.84, {}) == "healthy"
        assert memory_status(0.85, {}) == "degraded"
        assert memory_status(0.95, {}) == "down"

    def test_psi_escalates_even_when_anon_is_low(self):
        # benign reclaim (full60=0) stays healthy even at the live ~36% anon
        assert memory_status(0.36, {"full_avg60": 0.0}) == "healthy"
        # sustained stall is REAL pressure the anon metric alone would miss
        assert memory_status(0.36, {"full_avg60": 10.0}) == "degraded"
        assert memory_status(0.36, {"full_avg60": 30.0}) == "down"

    def test_worse_of_the_two_axes_wins(self):
        # anon degraded but PSI down → down
        assert memory_status(0.90, {"full_avg60": 35.0}) == "down"


class TestWorseStatus:
    def test_ranking(self):
        assert _worse_status("healthy", "down") == "down"
        assert _worse_status("degraded", "healthy") == "degraded"
        assert _worse_status("down", "degraded") == "down"
        assert _worse_status("healthy", "healthy") == "healthy"


class TestCollectCpuUsage:
    def test_shape_and_pressure_present(self):
        result = _collect_cpu_usage()
        assert set(result) >= {"status", "used_pct", "count", "pressure"}
        assert result["status"] in {"healthy", "degraded", "error", "unavailable"}
        assert isinstance(result["pressure"], dict)

    def test_baseline_first_call_is_healthy(self, monkeypatch):
        # Reset module state → first call is the baseline (used_pct=None → healthy)
        monkeypatch.setattr(infra, "_last_cpu_reading", None)
        first = _collect_cpu_usage()
        assert first["used_pct"] is None
        assert first["status"] == "healthy"

    def test_status_tracks_used_pct(self, monkeypatch):
        # Force a high-utilization delta and assert the status reflects it.
        monkeypatch.setattr(infra, "_last_cpu_reading", None)
        _collect_cpu_usage()  # establish a baseline
        # Rewrite the baseline so the next real /proc/stat delta looks ~100% busy:
        # a large elapsed total with the same idle → 100% used → error.
        idle, total, t = infra._last_cpu_reading
        monkeypatch.setattr(infra, "_last_cpu_reading", (idle, total - 10_000_000, t))
        result = _collect_cpu_usage()
        assert result["used_pct"] is not None and result["used_pct"] >= 95.0
        assert result["status"] == "error"
