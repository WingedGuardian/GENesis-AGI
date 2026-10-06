"""Verdict rules in genesis.hostmetrics.preflight, on synthetic snapshots."""

from __future__ import annotations

import pytest

from genesis.hostmetrics import __main__ as cli
from genesis.hostmetrics.host import HostMemory
from genesis.hostmetrics.preflight import (
    ASK,
    GIB,
    GO,
    NO,
    WAIT,
    Levers,
    Request,
    Snapshot,
    evaluate,
    group_disks,
    judge,
    load_levers,
)
from genesis.hostmetrics.readings import Memory

NO_HOST = HostMemory(unavailable="test")


def snap(
    *, mem_total=10, mem_used=2, cpu_cap=4.0, cpu_used=1.0, psi_mem=0.0, host=NO_HOST, disks=None
):
    return Snapshot(
        memory=Memory(mem_total * GIB, (mem_total - mem_used) * GIB, "cgroup"),
        host=host,
        cpu_capacity=cpu_cap,
        cpu_used=cpu_used,
        psi={"cpu": 0.0, "memory": psi_mem, "io": 0.0},
        disks=disks or {},
    )


def req(ram=1, cpu=100.0, **kw):
    return Request(name="job", ram=None if ram is None else ram * GIB, cpu=cpu, **kw)


def by(result, resource):
    return next(c for c in result.checks if c.resource == resource)


# ── one test per clause of judge() ───────────────────────────────────────────
def test_go_when_live_plus_estimate_fits():
    assert judge("m", total=10, live=2, estimate=5, threshold=0.8).verdict == GO


def test_wait_when_it_fits_alone_but_not_with_current_load():
    assert judge("m", total=10, live=5, estimate=3.5, threshold=0.8).verdict == WAIT


def test_no_when_estimate_alone_exceeds_the_line():
    assert judge("m", total=10, live=0, estimate=8.5, threshold=0.8).verdict == NO


def test_cpu_is_never_worse_than_wait():
    c = judge("cpu", total=400, live=0, estimate=1000, threshold=0.8, compressible=True)
    assert c.verdict == WAIT


def test_ask_when_over_the_line_with_sustained_pressure():
    c = judge("m", total=10, live=9, estimate=0.5, threshold=0.8, pressure_high=True)
    assert c.verdict == ASK


def test_over_the_line_without_pressure_is_wait():
    c = judge("m", total=10, live=9, estimate=0.5, threshold=0.8, pressure_high=False)
    assert c.verdict == WAIT


def test_persistent_resource_over_the_line_asks():
    assert judge("d", total=10, live=9, estimate=0.1, threshold=0.8, persistent=True).verdict == ASK


def test_approved_over_line_budgets_a_share_of_what_is_free():
    # 9 of 10 used: free 1, approved budget 0.8 × 1 = 0.8
    assert judge("m", total=10, live=9, estimate=0.7, threshold=0.8, approved=True).verdict == GO
    assert judge("m", total=10, live=9, estimate=0.9, threshold=0.8, approved=True).verdict == NO


# ── evaluate(): worst-of, legs, estimates ────────────────────────────────────
def test_overall_is_worst_of_resources():
    # memory GO, disk ASK (over the line) → ASK; NO elsewhere would outrank it.
    s = snap(disks={"/d": (10 * GIB, 1 * GIB)})
    r = evaluate(s, req(disks={"/d": GIB // 10}), Levers())
    assert (by(r, "memory").verdict, by(r, "disk /d").verdict, r.verdict) == (GO, ASK, ASK)
    r = evaluate(s, req(ram=9, disks={"/d": GIB // 10}), Levers())
    assert r.verdict == NO


def test_memory_pressure_lever_turns_over_line_into_ask():
    s = snap(mem_used=9, psi_mem=25.0)
    assert evaluate(s, req(), Levers(ask_psi=10.0)).verdict == ASK
    assert evaluate(s, req(), Levers(ask_psi=50.0)).verdict == WAIT


def test_host_leg_can_be_the_binding_one():
    host = HostMemory(total=20 * GIB, used=15 * GIB)  # host line 16: 15 + 2 > 16
    r = evaluate(snap(host=host), req(ram=2), Levers())
    assert by(r, "memory").verdict == GO
    assert by(r, "memory (host)").verdict == WAIT
    assert r.verdict == WAIT


def test_host_leg_unavailable_is_stated():
    r = evaluate(snap(host=HostMemory(unavailable="no link")), req(), Levers())
    assert r.verdict == GO
    assert any("host leg unavailable: no link" in n for n in r.notes)
    assert not any(c.resource == "memory (host)" for c in r.checks)


def test_unknown_estimate_is_no_without_assume_default():
    r = evaluate(snap(), req(ram=None), Levers())
    assert r.verdict == NO and r.checks == ()
    assert "estimate required: pass --ram" in r.notes[-1]


def test_assume_default_uses_the_levers_and_says_so():
    r = evaluate(
        snap(mem_total=10, cpu_cap=4.0),
        req(ram=None, cpu=None, assume_default=True),
        Levers(default_ram_pct=50.0, default_cpu_pct=25.0),
    )
    assert by(r, "memory").estimate == 5 * GIB
    assert by(r, "cpu").estimate == 100.0  # 25% of 4 cores, in core-percent
    assert sum("ASSUMED" in n for n in r.notes) == 2


def test_host_leg_still_judged_when_container_memory_unreadable():
    # The host would refuse this job; an unreadable container must not hide it.
    s = Snapshot(
        memory=None,
        host=HostMemory(total=10 * GIB, used=GIB),
        cpu_capacity=4.0,
        cpu_used=0.0,
        psi={},
        disks={},
    )
    r = evaluate(s, req(ram=9), Levers())
    assert by(r, "memory (host)").verdict == NO and r.verdict == NO


def test_unreadable_cpu_is_a_note_not_ask():
    s = Snapshot(
        memory=Memory(10 * GIB, 8 * GIB, "cgroup"),
        host=NO_HOST,
        cpu_capacity=4.0,
        cpu_used=None,
        psi={},
        disks={},
    )
    r = evaluate(s, req(), Levers())
    assert r.verdict == GO and any("CPU usage unreadable" in n for n in r.notes)


def test_disks_on_one_filesystem_are_summed():
    dev = {"/a": 1, "/b": 1, "/c": 2}.get
    disks, notes = group_disks([("/a", 30), ("/b", 30), ("/c", 5), ("/a", 1)], dev)
    assert disks == {"/a": 61, "/c": 5}
    assert notes == ["disk /b counted with /a (same filesystem)"]


def test_unreadable_memory_asks():
    s = Snapshot(memory=None, host=NO_HOST, cpu_capacity=4.0, cpu_used=0.0, psi={}, disks={})
    assert evaluate(s, req(), Levers()).verdict == ASK


def test_cpu_live_counts_in_core_percent():
    # 4 cores, 3 busy = 300 of 400; line 320; +100 → WAIT
    assert by(evaluate(snap(cpu_used=3.0), req(cpu=100.0), Levers()), "cpu").verdict == WAIT


# ── levers ───────────────────────────────────────────────────────────────────
def test_levers_env_file_then_env_wins(tmp_path):
    f = tmp_path / "rb.env"
    f.write_text("# comment\nGENESIS_RB_THRESHOLD_PCT=70\nGENESIS_RB_ASK_PSI='5'\nOTHER=1\n")
    lv = load_levers(env={"GENESIS_RB_THRESHOLD_PCT": "60"}, env_file=f)
    assert (lv.threshold_pct, lv.ask_psi, lv.notes) == (60.0, 5.0, ())


@pytest.mark.parametrize("bad", ["0", "150", "abc", "-5"])
def test_invalid_lever_falls_back_loudly(tmp_path, bad):
    lv = load_levers(env={"GENESIS_RB_THRESHOLD_PCT": bad}, env_file=tmp_path / "none")
    assert lv.threshold_pct == 80.0
    assert "GENESIS_RB_THRESHOLD_PCT" in lv.notes[0]


def test_levers_accept_export_lines_and_name_unknown_keys(tmp_path):
    f = tmp_path / "rb.env"
    f.write_text("export GENESIS_RB_THRESHOLD_PCT=50\nGENESIS_RB_TRESHOLD_PCT=60\n")
    lv = load_levers(env={}, env_file=f)
    assert lv.threshold_pct == 50.0
    assert lv.notes == ("GENESIS_RB_TRESHOLD_PCT is not a known lever; ignored",)


# ── CLI exit codes ───────────────────────────────────────────────────────────
@pytest.fixture
def fixed_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "take_snapshot", lambda paths, window, host=True: snap())
    monkeypatch.setattr(cli, "load_levers", lambda: Levers())


@pytest.mark.parametrize(("ram", "code"), [("1", 0), ("7", 3), ("9", 2)])
def test_cli_exit_codes(fixed_snapshot, capsys, ram, code):
    assert cli.main(["preflight", "--name", "j", "--ram", ram, "--cpu", "50"]) == code
    assert capsys.readouterr().out.split()[1] == "j"


def test_cli_usage_error_is_not_read_as_no(fixed_snapshot, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["preflight", "--name", "j", "--disk", "nogb"])
    assert exc.value.code == cli.EXIT_USAGE


def test_cli_json_carries_verdict_and_checks(fixed_snapshot, capsys):
    import json

    assert cli.main(["preflight", "--name", "j", "--ram", "1", "--cpu", "50", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "GO" and {c["resource"] for c in out["checks"]} == {"memory", "cpu"}


def test_cli_missing_estimate_takes_no_readings(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("snapshot taken before the estimate check")

    monkeypatch.setattr(cli, "take_snapshot", boom)
    monkeypatch.setattr(cli, "load_levers", lambda: Levers())
    assert cli.main(["preflight", "--name", "j", "--ram", "1"]) == 2
    assert "estimate required: pass --cpu" in capsys.readouterr().out


def test_cli_rejects_a_cpu_window_too_short_to_measure(fixed_snapshot):
    with pytest.raises(SystemExit) as exc:
        cli.main(["status", "--cpu-window", "0"])
    assert exc.value.code == cli.EXIT_USAGE


def test_cli_status_prints_each_reading(fixed_snapshot, capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "budget line: 80%" in out and "memory (cgroup): 2.0 GiB used of 10.0 GiB" in out
    assert "host memory: unavailable (test)" in out and "cpu: 1.00 of 4 cores busy" in out


def test_cli_status_json(fixed_snapshot, capsys):
    import json

    assert cli.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["threshold_pct"] == 80.0 and out["memory"]["source"] == "cgroup"


@pytest.mark.parametrize(
    "argv",
    [
        ["preflight", "--name", "j", "--ram", "1", "--cpu", "nan"],
        ["preflight", "--name", "j", "--ram", "nan", "--cpu", "50"],
        ["preflight", "--name", "j", "--ram", "inf", "--cpu", "50"],
        ["preflight", "--name", "j", "--ram", "1", "--cpu", "50", "--disk", "/x=inf"],
        ["preflight", "--name", "j", "--ram", "1", "--cpu", "50", "--disk", "/x=nan"],
        ["status", "--cpu-window", "nan"],
        ["status", "--cpu-window", "inf"],
        # finite but absurd: overflowed int(value * GiB) into a traceback (exit 1)
        ["preflight", "--name", "j", "--ram", "1e300", "--cpu", "50"],
        ["preflight", "--name", "j", "--ram", "1", "--cpu", "50", "--disk", "/x=1e300"],
        ["status", "--cpu-window", "1e300"],
        ["status", "--cpu-window", "86400"],
    ],
)
def test_non_finite_numbers_are_usage_errors(fixed_snapshot, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == cli.EXIT_USAGE


def test_missing_estimate_probes_no_disk(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("a disk path was probed before the estimate check")

    monkeypatch.setattr(cli.readings, "disk_device", boom)
    monkeypatch.setattr(cli, "load_levers", lambda: Levers())
    assert cli.main(["preflight", "--name", "j", "--ram", "1", "--disk", "/x=1"]) == 2
    assert "estimate required" in capsys.readouterr().out
