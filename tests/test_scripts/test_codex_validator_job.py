"""Refuse unsupported parent binding before entering the governor."""

import ctypes
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def job():
    return private_module("validator_job_under_test", ROOT / "scripts/codex_validator_job.py")


class Prctl:
    def __init__(self, result=0):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


def test_parent_binding_arms_correct_widths_then_checks_parent(job, monkeypatch):
    call = Prctl()
    loads = []

    def load(name, **kwargs):
        loads.append((name, kwargs))
        return SimpleNamespace(prctl=call)

    monkeypatch.setattr(job.ctypes, "CDLL", load)
    monkeypatch.setattr(job.os, "getppid", lambda: 4321)
    job.arm_parent_death(4321)
    assert loads == [(None, {"use_errno": True})]
    assert call.argtypes == [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    assert call.restype is ctypes.c_int
    assert call.calls == [(1, int(signal.SIGTERM), 0, 0, 0)]


@pytest.mark.parametrize("parent", [None, True, 0, 1, -1, "4321"])
def test_invalid_parent_refuses_before_loading_libc(job, monkeypatch, parent):
    monkeypatch.setattr(job.ctypes, "CDLL", lambda *a, **kw: pytest.fail("libc entered"))
    with pytest.raises(ValueError):
        job.arm_parent_death(parent)


@pytest.mark.parametrize("fault", ["platform", "libc", "prctl", "parent_changed"])
def test_binding_failure_never_enters_governor(job, monkeypatch, fault):
    monkeypatch.setattr(job.sys, "argv", ["job", "4321", "run"])
    monkeypatch.setattr(job.runpy, "run_module", lambda *a, **kw: pytest.fail("governor entered"))
    monkeypatch.setattr(job.os, "getppid", lambda: 1234 if fault == "parent_changed" else 4321)
    call = Prctl(-1 if fault == "prctl" else 0)

    def load(*args, **kwargs):
        if fault == "libc":
            raise OSError("unavailable")
        return SimpleNamespace(prctl=call)

    monkeypatch.setattr(job.ctypes, "CDLL", load)
    if fault == "platform":
        monkeypatch.setattr(job.sys, "platform", "unsupported")
    with pytest.raises(SystemExit, match="Validator job refused"):
        job.main()
    if fault == "parent_changed":
        assert call.calls, "close the death-before-arming race after arming"


def test_governor_runs_in_armed_process_with_existing_cli_arguments(job, monkeypatch):
    sequence = []
    monkeypatch.setattr(job.sys, "argv", ["job", str(os.getppid()), "run", "--name", "fixed"])
    monkeypatch.setattr(job, "arm_parent_death", lambda p: sequence.append(("armed", p)))
    monkeypatch.setattr(job.runpy, "run_module", lambda m, **kw: sequence.append((m, kw, list(job.sys.argv))))
    job.main()
    assert sequence == [("armed", os.getppid()), ("genesis.hostmetrics", {"run_name": "__main__"},
                                                 ["genesis.hostmetrics", "run", "--name", "fixed"])]
