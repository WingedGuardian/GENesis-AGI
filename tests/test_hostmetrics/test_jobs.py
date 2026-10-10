"""Live genesis-job scopes as read from systemd, and the reservation they hold."""

from __future__ import annotations

import pytest

from genesis.hostmetrics import jobs
from genesis.hostmetrics.jobs import Job

MIB = 1024 * 1024

# The shape of `systemctl --user show -p Id -p MemoryMax -p MemoryCurrent
# -p CPUQuotaPerSecUSec` over several units (systemd 255).
_SHOW = """ControlGroup=/a
MemoryCurrent=104857600
CPUQuotaPerSecUSec=500ms
MemoryMax=314572800
Id=genesis-job-build-a1b2c3.scope

MemoryCurrent=[not set]
CPUQuotaPerSecUSec=1.500000s
MemoryMax=209715200
Id=genesis-job-test-d4e5f6.scope

MemoryCurrent=5000
CPUQuotaPerSecUSec=infinity
MemoryMax=infinity
Id=genesis-job-odd-000000.scope

MemoryCurrent=1
MemoryMax=100
Id=run-u42.scope
"""


def test_parse_show_reads_reservations_and_skips_others():
    parsed = jobs.parse_show(_SHOW, file_cache=lambda cg: 0)
    assert parsed == [
        Job("genesis-job-build-a1b2c3.scope", 300 * MIB, 100 * MIB, 50.0),
        Job("genesis-job-test-d4e5f6.scope", 200 * MIB, 0, 150.0),
    ]


@pytest.mark.parametrize(
    ("value", "pct"),
    [("500ms", 50.0), ("2s", 200.0), ("250000us", 25.0), ("infinity", None), ("1min 30s", 9000.0)],
)
def test_cpu_quota_units(value, pct):
    assert jobs._cpu_pct(value) == pct


def test_reserved_beyond_use_counts_only_the_unused_part():
    # Cancellation control: a job registered but not started (current 0) and one
    # running below its cap hold the same total once its use is in the live reading.
    not_started = [Job("genesis-job-a-1.scope", 300, 0, None)]
    running = [Job("genesis-job-a-1.scope", 300, 120, None)]
    assert jobs.reserved_beyond_use(not_started) == 300
    assert jobs.reserved_beyond_use(running) + 120 == 300  # live 120 + unused 180
    over = [Job("genesis-job-a-1.scope", 300, 350, None)]  # cannot happen under MemoryMax
    assert jobs.reserved_beyond_use(over) == 0


def test_live_jobs_none_when_manager_unreachable(monkeypatch):
    monkeypatch.setattr(jobs, "_systemctl", lambda *a: None)
    assert jobs.live_jobs() is None


def test_live_jobs_lists_then_shows(monkeypatch):
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "list-units":
            return "genesis-job-build-a1b2c3.scope loaded active running /bin/sh -c x\n"
        return _SHOW.split("\n\n")[0]

    monkeypatch.setattr(jobs, "_systemctl", fake)
    assert [j.unit for j in jobs.live_jobs()] == ["genesis-job-build-a1b2c3.scope"]
    assert calls[1][-1] == "genesis-job-build-a1b2c3.scope"
    for prop in ("Id", "MemoryMax", "MemoryCurrent", "CPUQuotaPerSecUSec", "ControlGroup"):
        assert prop in calls[1]  # ControlGroup is what lets page cache be subtracted


def test_systemd_env_fills_missing_bus_variables(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/custom")
    env = jobs.systemd_env()
    assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/custom"  # an existing one is kept


def test_page_cache_does_not_shrink_a_reservation():
    # MEASURED shape: a scope that wrote 100 MiB showed MemoryCurrent ≈ 100 MiB, nearly all
    # inactive_file. Container "used" excludes that cache, so the job still holds ~R.
    block = _SHOW.split("\n\n")[0]  # MemoryMax 300 MiB, MemoryCurrent 100 MiB, ControlGroup /a
    (job,) = jobs.parse_show(block, file_cache=lambda cg: 99 * MIB if cg == "/a" else 0)
    assert job.current == 1 * MIB
    assert jobs.reserved_beyond_use([job]) == 299 * MIB


def test_file_cache_reads_the_scope_cgroup(tmp_path, monkeypatch):
    scope = tmp_path / "app.slice" / "genesis-job-x-1.scope"
    scope.mkdir(parents=True)
    (scope / "memory.stat").write_text("anon 10\ninactive_file 300\nactive_file 200\n")
    monkeypatch.setattr(jobs, "CGROUP_ROOT", tmp_path)
    assert jobs._file_cache("/app.slice/genesis-job-x-1.scope") == 500
    assert jobs._file_cache("") is None


def test_listed_but_unshowable_jobs_are_unknown(monkeypatch):
    def fake(*args):
        return (
            "genesis-job-a-1.scope loaded active running x\n" if args[0] == "list-units" else None
        )

    monkeypatch.setattr(jobs, "_systemctl", fake)
    assert jobs.live_jobs() is None


def test_unreadable_cache_keeps_the_whole_reservation():
    block = _SHOW.split("\n\n")[0]  # MemoryMax 300 MiB, MemoryCurrent 100 MiB
    (job,) = jobs.parse_show(block, file_cache=lambda cg: None)
    assert job.current == 0 and jobs.reserved_beyond_use([job]) == 300 * MIB


def test_systemd_env_replaces_empty_bus_variables(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "")
    env = jobs.systemd_env()
    assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/run/user/")
