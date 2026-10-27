"""Tiny real worker proves the ledger, enforced caps and scope wall deadline."""

import fcntl
import shutil
import subprocess
import sys

import pytest

from genesis.hostmetrics import jobs, run
from genesis.transcript_analytics import resources


def test_real_named_scope_is_reserved_and_times_out(tmp_path, monkeypatch):
    if not shutil.which("systemd-run"):
        pytest.skip("systemd-run unavailable")
    probe = subprocess.run(
        ["systemctl", "--user", "show-environment"],
        env=jobs.systemd_env(),
        capture_output=True,
        timeout=5,
    )
    if probe.returncode:
        pytest.skip("systemd user manager unavailable")
    original = run.scope_argv
    launched = []
    ram = 128 * 1024**2
    worker = (
        "import os,time; from genesis.transcript_analytics import resources as r; "
        "r._acknowledge(r._enforced(os.environ[r._CHILD])); time.sleep(10)"
    )

    def smoke_argv(unit, props, slice_name, *trailing):
        launched.append(unit)
        assert "RuntimeMaxSec=1h" in props
        short_props = ["RuntimeMaxSec=2s" if prop == "RuntimeMaxSec=1h" else prop for prop in props]
        return original(unit, short_props, slice_name, sys.executable, "-c", worker)

    monkeypatch.setattr(run, "scope_argv", smoke_argv)
    real_ready = resources._await_ready

    def confirmed(proc, descriptor):
        assert real_ready(proc, descriptor)
        live = jobs.live_jobs()
        assert live is not None
        matching = [job for job in live if job.unit == launched[0] + ".scope"]
        assert len(matching) == 1 and matching[0].reserved == ram
        assert jobs.reserved_beyond_use(matching) > 0
        return True

    monkeypatch.setattr(resources, "_await_ready", confirmed)
    with (tmp_path / "admission.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        code = resources._launch([], ram, 25, lease)
    assert code == 143  # SIGTERM from the enforced two-second scope deadline
    assert launched[0].startswith("genesis-job-transcript-analytics-")
