"""The watchdog names the processes behind an I/O stall (identity at the event)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import genesis.autonomy.watchdog as wd


def _checker(tmp_path: Path) -> wd.WatchdogChecker:
    return wd.WatchdogChecker(
        status_file=str(tmp_path / "status.json"),
        secrets_path=str(tmp_path / "secrets.env"),
        state_file=str(tmp_path / "watchdog_state.json"),
        config_validation=False,
    )


def _pressure(monkeypatch: pytest.MonkeyPatch, full_avg10: float) -> None:
    text = (
        "some avg10=1.00 avg60=1.00 avg300=1.00 total=1\n"
        f"full avg10={full_avg10:.2f} avg60=1.00 avg300=1.00 total=1\n"
    )

    class _P:
        def __init__(self, *_a: object) -> None:
            pass

        def read_text(self) -> str:
            return text

    monkeypatch.setattr(wd, "Path", _P)


@pytest.mark.parametrize(("avg10", "sampled"), [(10.0, False), (25.0, False), (30.0, True), (61.5, True)])
def test_culprits_are_sampled_only_above_the_warning_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, avg10: float, sampled: bool
) -> None:
    checker = _checker(tmp_path)
    calls: list[float] = []
    monkeypatch.setattr(checker, "_log_io_culprits", calls.append)
    _pressure(monkeypatch, avg10)
    checker._check_io_pressure()
    assert calls == ([avg10] if sampled else [])


def test_culprit_line_names_process_unit_and_readable_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import genesis.util.proc_io as proc_io

    checker = _checker(tmp_path)
    monkeypatch.setattr(wd.os, "listdir", lambda _p: ["1", "2", "3", "self", "net"])
    monkeypatch.setattr(proc_io, "rank_by_io_rate", lambda pids, **_k: ([
        {"pid": 2, "comm": "gpg", "read_rate": 0.0, "write_rate": 40e6, "total_rate": 40e6},
        {"pid": 4, "comm": "python3", "read_rate": 2_000.0, "write_rate": 0.0, "total_rate": 2_000.0},
        {"pid": 5, "comm": "claude", "read_rate": 0.0, "write_rate": 3_000.0, "total_rate": 3_000.0},
        {"pid": 1, "comm": "idle", "read_rate": 0.0, "write_rate": 0.0, "total_rate": 0.0},
    ], 2, 40_005_000.0))
    monkeypatch.setattr(proc_io, "systemd_unit", lambda pid: "genesis-backup.service" if pid == 2 else None)
    monkeypatch.setattr(proc_io, "claude_ancestor", lambda pid: 777 if pid in (4, 5) else None)
    with caplog.at_level(logging.WARNING, logger=wd.logger.name):
        checker._log_io_culprits(44.5)
    line = caplog.records[-1].getMessage()
    assert "full avg10=44.5%" in line
    assert "2 of 3 readable" in line
    assert "total 40.0MB/s" in line
    assert "this container's readable processes only" in line
    assert "gpg[2] genesis-backup.service r=0KB/s w=40.0MB/s" in line
    assert "python3[4] ? <-claude[777] r=2KB/s w=0KB/s" in line
    assert "idle" not in line  # a zero-rate process is not listed
    assert "claude[5] ? r=0KB/s w=3KB/s" in line  # a claude process is not its own session


def test_no_busy_process_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When the I/O is outside this container, the line must not read as empty."""
    import genesis.util.proc_io as proc_io

    checker = _checker(tmp_path)
    monkeypatch.setattr(wd.os, "listdir", lambda _p: ["1"])
    monkeypatch.setattr(proc_io, "rank_by_io_rate", lambda pids, **_k: ([], 0, 0.0))
    with caplog.at_level(logging.WARNING, logger=wd.logger.name):
        checker._log_io_culprits(30.0)
    line = caplog.records[-1].getMessage()
    assert "none did I/O in the sample" in line
    assert "total 0KB/s" in line


def test_a_sampling_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import genesis.util.proc_io as proc_io

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("proc unreadable")

    checker = _checker(tmp_path)
    monkeypatch.setattr(proc_io, "rank_by_io_rate", _boom)
    with caplog.at_level(logging.WARNING, logger=wd.logger.name):
        checker._log_io_culprits(30.0)
    assert "I/O culprit sample failed" in caplog.records[-1].getMessage()


def test_a_formatting_failure_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Everything after the sample is inside the try too: the tick must never die here."""
    import genesis.util.proc_io as proc_io

    def _boom(_pid: int) -> None:
        raise RuntimeError("cgroup parse")

    checker = _checker(tmp_path)
    monkeypatch.setattr(wd.os, "listdir", lambda _p: ["1"])
    monkeypatch.setattr(proc_io, "rank_by_io_rate", lambda pids, **_k: (
        [{"pid": 1, "comm": "x", "read_rate": 1.0, "write_rate": 1.0, "total_rate": 2.0}], 1, 2.0))
    monkeypatch.setattr(proc_io, "systemd_unit", _boom)
    with caplog.at_level(logging.WARNING, logger=wd.logger.name):
        checker._log_io_culprits(30.0)
    assert "I/O culprit sample failed" in caplog.records[-1].getMessage()
