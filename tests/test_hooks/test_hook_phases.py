"""scripts/hooks/hook_phases.py — the per-step timing line a hook writes to stderr."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import hook_phases as hp  # noqa: E402


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _payload(line: str) -> dict:
    assert line.startswith(hp.PHASE_LINE_PREFIX), line
    assert line.endswith("\n") and line.count("\n") == 1, "one line, newline-terminated"
    return json.loads(line[len(hp.PHASE_LINE_PREFIX) :])


def _psi_root(tmp_path: Path) -> Path:
    (tmp_path / "io.pressure").write_text(
        "some avg10=1.50 avg60=15.60 avg300=23.28 total=1\n"
        "full avg10=0.48 avg60=6.79 avg300=11.20 total=2\n"
    )
    (tmp_path / "cpu.pressure").write_text(
        "some avg10=15.51 avg60=15.86 avg300=11.95 total=3\n"
        "full avg10=2.09 avg60=1.88 avg300=1.45 total=4\n"
    )
    (tmp_path / "memory.pressure").write_text(
        "some avg10=0.65 avg60=0.39 avg300=0.61 total=5\n"
        "full avg10=0.25 avg60=0.36 avg300=0.46 total=6\n"
    )
    return tmp_path


def test_each_phase_runs_until_the_next_mark_and_the_last_until_now(tmp_path) -> None:
    clock = _FakeClock()
    pc = hp.PhaseClock(age_s=0.4, clock=clock, psi_root=tmp_path)
    pc.mark("a")
    clock.now += 0.25
    pc.mark("b")
    clock.now += 1.0
    p = _payload(pc.line())
    assert p["phases"] == [["a", 250.0], ["b", 1000.0]]
    assert p["age0_s"] == 0.4
    assert p["total_s"] == 1.65  # 0.4 before the clock started + 1.25 since
    assert p["cut"] is False
    assert "running" not in p


def test_a_cut_line_names_the_step_that_was_running(tmp_path) -> None:
    clock = _FakeClock()
    pc = hp.PhaseClock(age_s=None, clock=clock, psi_root=tmp_path)
    pc.mark("heartbeat_write")
    clock.now += 8.0
    p = _payload(pc.line(cut=True))
    assert p["cut"] is True
    assert p["running"] == "heartbeat_write"
    assert p["running_ms"] == 8000.0
    assert p["age0_s"] is None
    assert p["total_s"] == 8.0  # unknown age adds nothing


def test_fields_and_extra_fields_are_carried_and_long_text_is_bounded(tmp_path) -> None:
    pc = hp.PhaseClock(clock=_FakeClock(), psi_root=tmp_path)
    pc.set(path="degraded", server_reason="x" * 500, timed_out=False)
    p = _payload(pc.line(recall_landed=True))
    assert p["path"] == "degraded"
    assert p["timed_out"] is False
    assert p["recall_landed"] is True
    assert len(p["server_reason"]) == hp.MAX_FIELD_CHARS


def test_psi_is_read_from_the_cgroup_files(tmp_path) -> None:
    pc = hp.PhaseClock(clock=_FakeClock(), psi_root=_psi_root(tmp_path))
    assert _payload(pc.line())["psi"] == {
        "io_some10": 1.5,
        "io_some60": 15.6,
        "io_full10": 0.48,
        "io_full60": 6.79,
        "cpu_some10": 15.51,
        "mem_full10": 0.25,
    }


def test_unreadable_or_malformed_psi_is_null_not_zero(tmp_path) -> None:
    (tmp_path / "io.pressure").write_text("some avg10=junk avg60=2.00\n")
    psi = _payload(hp.PhaseClock(clock=_FakeClock(), psi_root=tmp_path).line())["psi"]
    assert psi["io_some10"] is None  # malformed
    assert psi["io_some60"] == 2.0
    assert psi["cpu_some10"] is None  # file absent
    assert psi["mem_full10"] is None


def test_the_line_never_exceeds_its_byte_cap(tmp_path) -> None:
    clock = _FakeClock()
    pc = hp.PhaseClock(age_s=1.0, clock=clock, psi_root=_psi_root(tmp_path))
    for i in range(200):
        pc.mark(f"phase_with_a_long_name_{i}")
        clock.now += 0.001
    pc.set(path="degraded", server_reason="r" * 500, timed_out=True, deadline_hit=True)
    line = pc.line(cut=True, recall_landed=False, flushed=False)
    assert len(line.encode("utf-8")) <= hp.MAX_LINE_BYTES
    p = _payload(line)
    # The collapsed head is named and counted; the newest phase (the running one) survives.
    assert p["phases"][0][0].startswith("+")
    assert p["phases"][-1][0] == "phase_with_a_long_name_199"
    assert p["running"] == "phase_with_a_long_name_199"
    collapsed = int(p["phases"][0][0][1:])
    assert collapsed + len(p["phases"]) - 1 == 200


def test_reset_run_keeps_module_marks_and_drops_run_state(tmp_path) -> None:
    pc = hp.PhaseClock(clock=_FakeClock(), psi_root=tmp_path)
    pc.mark("imports")
    pc.freeze_module()
    pc.mark("stdin")
    pc.set(path="server")
    pc.reset_run()
    p = _payload(pc.line())
    assert [name for name, _ in p["phases"]] == ["imports"]
    assert "path" not in p


def test_line_is_safe_while_another_thread_keeps_marking(tmp_path) -> None:
    """The hard stop calls line() on its timer thread while the main thread may mark."""
    pc = hp.PhaseClock(psi_root=tmp_path)
    done = threading.Event()

    def _marker() -> None:
        for i in range(20_000):
            pc.mark(f"m{i % 50}")
            pc.set(**{f"k{i % 7}": i})
        done.set()

    t = threading.Thread(target=_marker, daemon=True)
    t.start()
    rendered = 0
    while not done.is_set() or rendered == 0:
        line = pc.line(cut=True)
        assert len(line.encode("utf-8")) <= hp.MAX_LINE_BYTES
        _payload(line)
        rendered += 1
    t.join(timeout=5)
    assert rendered > 0


def test_an_empty_clock_still_renders(tmp_path) -> None:
    p = _payload(hp.PhaseClock(clock=_FakeClock(), psi_root=tmp_path).line(cut=True))
    assert p["phases"] == []
    assert "running" not in p


def test_a_folded_line_keeps_its_content_when_phases_are_long(tmp_path) -> None:
    """Review SF-1: the fold entry's width was estimated at 0.0 ms. With large per-step
    times the real "+N" entry is wider, and the line collapsed to a contentless stub."""
    for n in range(10, 200):
        for ms in (0.1, 1234.5, 99999.9):
            for width in (5, 10, 20, 30, 40):
                clock = _FakeClock()
                pc = hp.PhaseClock(age_s=1.0, clock=clock, psi_root=_psi_root(tmp_path))
                for i in range(n):
                    pc.mark(f"{i:0{width}d}")
                    clock.now += ms / 1000
                pc.set(path="degraded", server_reason="r" * 80)
                line = pc.line(cut=True, recall_landed=False, flushed=False)
                assert len(line.encode("utf-8")) <= hp.MAX_LINE_BYTES
                p = _payload(line)
                assert "truncated" not in p, (n, ms, width)
                assert p["running"] == f"{n - 1:0{width}d}"
