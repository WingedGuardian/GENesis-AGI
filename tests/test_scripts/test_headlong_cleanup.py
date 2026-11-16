"""B cleanup evidence uses only directly owned harmless processes on the host."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ci"))
    import headlong_monolith_controls

    return headlong_monolith_controls


@pytest.mark.parametrize("code", [0, 7])
def test_actual_command_exit_has_measured_group_closure(module, tmp_path, code):
    with module.command_records() as records:
        result = module.command([sys.executable, "-c", f"print('fixture'); raise SystemExit({code})"],
                                os.environ.copy(), tmp_path)
    assert result == {"returncode": code, "stdout": "fixture\n", "stderr": "", "timed_out": False}
    assert len(records) == 1 and records[0]["returncode"] == code
    assert type(records[0]["pgid"]) is int and records[0]["pgid"] > 1
    assert records[0]["group_closed"] is True and records[0]["probe_error"] is None
    assert module.closed_commands(records)


def test_actual_timeout_records_cleanup_without_qualifying_success(module, tmp_path):
    with module.command_records() as records:
        result = module.command([sys.executable, "-c", "import signal; signal.pause()"],
                                os.environ.copy(), tmp_path, timeout=0.05)
    assert result["timed_out"] is True and result["returncode"] == -9
    assert records[0]["group_closed"] is True
    assert not module.closed_commands(records)


def test_a_slow_post_kill_drain_still_reaps_the_leader(module, monkeypatch, tmp_path):
    # A loaded runner can outlast the post-kill communicate() budget. The killed
    # leader must still be reaped before the group probe: an unreaped zombie is a
    # group member, and the probe would record a closed group as alive (#4555).
    real_popen = module.subprocess.Popen

    class SlowDrain(real_popen):
        calls = 0

        def communicate(self, *args, **kwargs):
            SlowDrain.calls += 1
            if SlowDrain.calls == 1:
                return super().communicate(*args, **kwargs)
            raise subprocess.TimeoutExpired(self.args, kwargs.get("timeout"), output="", stderr="")

    monkeypatch.setattr(module.subprocess, "Popen", SlowDrain)
    with module.command_records() as records:
        result = module.command([sys.executable, "-c", "import signal; signal.pause()"],
                                os.environ.copy(), tmp_path, timeout=0.2)
    assert SlowDrain.calls == 2 and result["timed_out"] is True
    assert records[0]["group_closed"] is True and records[0]["probe_error"] is None


@pytest.mark.parametrize("problem", ["survivor", "permission"])
def test_live_or_unverifiable_group_never_becomes_clean(module, monkeypatch, tmp_path, problem):
    def group_alive(pgid):
        if problem == "permission":
            raise PermissionError("synthetic inaccessible scope")
        return True

    monkeypatch.setattr(module.processes, "group_alive", group_alive)
    signals = []
    monkeypatch.setattr(module.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    with module.command_records() as records:
        module.command([sys.executable, "-c", "pass"], os.environ.copy(), tmp_path)
    assert records[0]["group_closed"] is False
    assert not module.closed_commands(records)
    assert bool(records[0]["probe_error"]) is (problem == "permission")
    assert all(pgid == records[0]["pgid"] for pgid, _ in signals)


@pytest.mark.parametrize("records", [[], [{}], [{"pgid": True, "group_closed": True}],
                                      [{"pgid": 123, "group_closed": 1}]])
def test_missing_or_malformed_command_evidence_is_not_clean(module, records):
    assert module.closed_commands(records) is False


@pytest.mark.parametrize("field", ["argv", "pgid", "returncode", "timed_out", "group_closed", "probe_error"])
def test_every_command_field_is_required(module, field):
    row = {"argv": ["fixture"], "pgid": 123, "returncode": 0, "timed_out": False,
           "group_closed": True, "probe_error": None}
    assert module.closed_commands([row])
    del row[field]
    assert module.closed_commands([row]) is False


def test_shared_callers_do_not_inherit_B_probe_state(module, monkeypatch, tmp_path):
    def forbidden_probe(pgid):
        raise AssertionError("B-only probe must not run outside its collector")

    monkeypatch.setattr(module.processes, "group_alive", forbidden_probe)
    result = module.command([sys.executable, "-c", "print('fixture')"], os.environ.copy(), tmp_path)
    assert result == {"returncode": 0, "stdout": "fixture\n", "stderr": "", "timed_out": False}


async def test_scenario_cannot_pass_without_recorded_commands(module, monkeypatch, tmp_path):
    async def scenario(root, name):
        return {"scenario": name, "control_passed": True}

    monkeypatch.setattr(module, "_run_scenario", scenario)
    result = await module.run_scenario(tmp_path, "effect")
    assert result["control_passed"] is False and result["cleanup_confirmed"] is False


def test_correlation_cannot_pass_without_recorded_commands(module, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_correlation_controls", lambda root: {"passed": True})
    result = module.correlation_controls(tmp_path)
    assert result["passed"] is False and result["cleanup_confirmed"] is False


def test_nested_command_collectors_restore_after_failure(module, tmp_path):
    with module.command_records() as outer:
        with pytest.raises(ValueError, match="synthetic"), module.command_records() as inner:
            module.command([sys.executable, "-c", "pass"], os.environ.copy(), tmp_path)
            raise ValueError("synthetic interruption")
        module.command([sys.executable, "-c", "pass"], os.environ.copy(), tmp_path)
    assert len(inner) == len(outer) == 1
    assert inner[0]["pgid"] != outer[0]["pgid"]
    assert module.closed_commands(inner) and module.closed_commands(outer)


def test_bounded_pipe_drain_failure_retains_incomplete_evidence(module, monkeypatch, tmp_path):
    # Both pipe timeouts are simulated; no host descendant is orphaned.
    class Process:
        pid = os.getpid() + 10000
        returncode = -9
        stdin = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def communicate(self, *args, **kwargs):
            assert "timeout" in kwargs
            raise subprocess.TimeoutExpired("fixture", kwargs["timeout"], output=b"partial", stderr=b"diagnostic")

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(module.os, "killpg", lambda *args: None)
    monkeypatch.setattr(module.processes, "group_alive", lambda pgid: True)
    with module.command_records() as records:
        result = module.command(["fixture"], {}, tmp_path)
    assert result["timed_out"] is True
    assert result["stdout"] == "partial" and result["stderr"] == "diagnostic"
    assert records[0]["group_closed"] is False and not module.closed_commands(records)
