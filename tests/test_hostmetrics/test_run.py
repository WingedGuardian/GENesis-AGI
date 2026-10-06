"""The run wrapper: scope argv, the probe ladder, the reporter, the watchdog, the CLI flow."""

from __future__ import annotations

import os
import subprocess

import pytest

from genesis.hostmetrics import __main__ as cli
from genesis.hostmetrics import run
from genesis.hostmetrics.preflight import GO, NO, WAIT, Check, Levers, Request, Result
from genesis.hostmetrics.readings import Memory


def test_unit_name_is_sanitised_and_prefixed():
    assert run.unit_name("my build/x;rm", "a1b2c3") == "genesis-job-my_build_x_rm-a1b2c3"
    assert run.unit_name("", "a1b2c3") == "genesis-job-job-a1b2c3"


def test_properties_carry_the_estimates_as_caps():
    props = run.scope_properties(512 * 2**20, 150.4)
    assert props == [
        "MemoryMax=536870912",
        "MemorySwapMax=0",
        "CPUQuota=150%",
        "IOWeight=50",
        "OOMPolicy=continue",
    ]
    assert "OOMPolicy=continue" not in run.scope_properties(1, 0.2, oom_continue=False)
    assert "CPUQuota=1%" in run.scope_properties(1, 0.2)  # systemd rejects 0%


def _runner(results):
    """A fake systemd-run: each probe gets the next (returncode, stderr) or exception."""
    seen = []

    def fake(argv, **kw):
        seen.append(argv)
        result = results[len(seen) - 1]
        if isinstance(result, BaseException):
            raise result
        code, err = result
        return subprocess.CompletedProcess(argv, code, b"", err.encode())

    return fake, seen


@pytest.fixture
def has_systemd_run(monkeypatch):
    monkeypatch.setattr(run.shutil, "which", lambda name: "/usr/bin/systemd-run")


def _props(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]


def test_ladder_takes_full_properties_when_accepted(has_systemd_run):
    fake, seen = _runner([(0, "")])
    caps = run.choose_properties("u", 2**30, 100, "s.slice", fake)
    assert caps.props == run.scope_properties(2**30, 100) and _props(seen[0]) == caps.props
    assert "--slice=s.slice" in seen[0] and "--collect" in seen[0]
    assert seen[0][-3:] == ["/bin/sh", "-c", run._ENFORCEMENT_SH]


def test_ladder_drops_oom_policy_only_when_systemd_names_it(has_systemd_run):
    # systemd before 253 answers "Unknown assignment: OOMPolicy=continue" for a scope.
    fake, seen = _runner([(1, "Unknown assignment: OOMPolicy=continue"), (0, "")])
    caps = run.choose_properties("u", 2**30, 100, None, fake)
    assert caps.props == run.scope_properties(2**30, 100, oom_continue=False)
    assert [_props(a) for a in seen] == [run.scope_properties(2**30, 100), caps.props]
    assert len({a[5] for a in seen}) == 2  # a distinct probe unit per rung


@pytest.mark.parametrize(
    "outcome",
    [(1, "Failed to connect to bus: No such file or directory"), FileNotFoundError("gone")],
)
def test_unreachable_manager_runs_uncapped(has_systemd_run, outcome):
    assert run.choose_properties("u", 2**30, 100, None, _runner([outcome])[0]) is None


def test_a_slow_probe_is_refused_not_uncapped(has_systemd_run):
    # A probe that times out says nothing about whether the manager exists; running
    # the job uncapped on that guess is what the probe is for preventing.
    fake, seen = _runner([subprocess.TimeoutExpired("systemd-run", 15), (0, "")])
    with pytest.raises(run.ProbeRefused, match="did not finish"):
        run.choose_properties("u", 2**30, 100, None, fake)
    assert seen[1][:4] == ["systemctl", "--user", "stop", "--no-block"]  # probe cleaned up
    assert seen[1][-1] == "u-probe0.scope"


def test_systemd_run_that_cannot_execute_is_refused(has_systemd_run):
    with pytest.raises(run.ProbeRefused, match="could not run"):
        run.choose_properties("u", 2**30, 100, None, _runner([PermissionError("noexec")])[0])


def test_a_refused_property_is_an_error_not_uncapped(has_systemd_run):
    # MEASURED: MemoryMax=0 → "Failed to start transient scope unit: Value specified in
    # MemoryMax is out of range". Silently running uncapped would hide the job.
    err = "Failed to start transient scope unit: Value specified in MemoryMax is out of range"
    with pytest.raises(run.ProbeRefused, match="out of range"):
        run.choose_properties("u", 0, 100, None, _runner([(1, err)])[0])
    with pytest.raises(run.ProbeRefused, match="exited 137"):
        run.choose_properties("u", 2**20, 100, None, _runner([(137, "")])[0])


@pytest.mark.parametrize(
    ("stdout", "unenforced", "unverified"),
    [
        (b"214745088\n20000 100000\n", (), ()),  # MEASURED: page-rounded MemoryMax, 20% quota
        (b"max\n20000 100000\n", ("memory",), ()),
        (b"214745088\nmax 100000\n", ("cpu",), ()),
        (b"missing\nmissing\n", (), ("memory", "cpu")),  # e.g. cgroup v1
        (b"", (), ("memory", "cpu")),
    ],
)
def test_probe_reports_caps_the_kernel_does_not_apply(
    has_systemd_run, stdout, unenforced, unverified
):
    def fake(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    caps = run.choose_properties("u", 2**30, 100, None, fake)
    assert (caps.unenforced, caps.unverified) == (unenforced, unverified)


def test_no_systemd_run_means_uncapped(monkeypatch):
    monkeypatch.setattr(run.shutil, "which", lambda name: None)
    assert run.choose_properties("u", 1, 1, None, _runner([(0, "")])[0]) is None


class _Proc:
    pid = 4242  # explicit: a mock pid of 1 would make killpg hit every process

    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw

    def wait(self):
        return 0


def test_launch_runs_the_probed_properties(monkeypatch, capsys):
    props = run.scope_properties(2**30, 100)
    launched = []
    monkeypatch.setattr(run, "choose_properties", lambda *a: run.Caps(props))
    monkeypatch.setattr(
        run.subprocess,
        "Popen",
        lambda argv, **kw: launched.append(_Proc(argv, **kw)) or launched[-1],
    )
    assert run.launch("j", ["make"], 2**30, 100, lambda: None) == 0
    argv = launched[0].argv
    assert _props(argv) == props and argv[argv.index("--") + 1 :][:2] == ["/bin/sh", "-c"]
    assert argv[-1] == "make" and launched[0].kw["pass_fds"]
    assert "genesis-job genesis-job-j-" in capsys.readouterr().err


def test_launch_uncapped_runs_the_command_for_real(monkeypatch, capsys):
    monkeypatch.setattr(run, "choose_properties", lambda *a: None)
    assert run.launch("j", ["sh", "-c", "exit 3"], 2**30, 100, lambda: None) == 3
    err = capsys.readouterr().err
    assert "UNCAPPED" in err and "exit 3" in err and "largest process" in err


def test_launch_uncapped_missing_command_is_127(monkeypatch):
    monkeypatch.setattr(run, "choose_properties", lambda *a: None)
    assert run.launch("j", ["/nonexistent/cmd"], 2**30, 100, lambda: None) == 127


def test_stop_scope_never_raises_and_does_not_wait():
    calls = []

    def slow(argv, **kw):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 15)

    run.stop_scope("genesis-job-j-1", slow)  # must not raise
    assert calls[0][:4] == ["systemctl", "--user", "stop", "--no-block"]


def test_parse_report():
    assert run.parse_report(b"104857600 162177 1\n") == (104857600, 0.162177, 1)
    assert run.parse_report(b"") == (None, None, None)
    assert run.parse_report(b"66433024 147686 \n") == (66433024, 0.147686, None)


def test_watchdog_stops_only_after_the_grace_period():
    over = iter([None, "mem", "mem", None, "mem", "mem", "mem"])
    stops = []
    dog = run.Watchdog(lambda: next(over), lambda: stops.append(1), grace=60)
    assert [dog.tick(t) for t in (0, 10, 50)] == [False, False, False]  # over 40 s so far
    assert dog.tick(80) is False  # dipped under: the clock resets
    assert [dog.tick(t) for t in (90, 140, 150)] == [False, False, True]
    assert stops == [1] and dog.fired == "mem for 60s"


def test_kill_group_never_signals_group_zero_or_one(monkeypatch):
    sent = []
    monkeypatch.setattr(run.os, "killpg", lambda pgid, sig: sent.append(pgid))
    run.kill_group(0)
    run.kill_group(1)
    run.kill_group(4242)
    assert sent == [4242]


def test_over_the_line_checks_memory_and_disks_unless_approved():
    mem = Memory(total=100, available=10, source="cgroup")  # 90 used > 80 line
    check = run.over_the_line(0.8, ["/d"], frozenset(), lambda: mem, lambda p: (100, 50))
    assert check() == "container memory over the line"
    check = run.over_the_line(0.8, ["/d"], frozenset({"memory"}), lambda: mem, lambda p: (100, 5))
    assert check() == "disk /d over the line"
    check = run.over_the_line(
        0.8, ["/d"], frozenset({"memory", "disk"}), lambda: mem, lambda p: (100, 5)
    )
    assert check() is None


# ── the CLI flow ─────────────────────────────────────────────────────────────
def _result(verdict):
    checks = (
        Check("memory", verdict, 10, 1, 2**30, 8, "x"),
        Check("cpu", GO, 400, 0, 50.0, 320, "x"),
    )
    return Result(verdict, checks, ())


@pytest.fixture
def flow(monkeypatch):
    verdicts, launched = [], []

    def decide(args):
        return Request(name=args.name), Levers(), _result(verdicts.pop(0))

    monkeypatch.setattr(cli, "_decide", decide)
    monkeypatch.setattr(cli, "_POLL_SECS", 0.0)
    monkeypatch.setattr(
        run,
        "launch",
        lambda name, cmd, ram, cpu, watch, sl: launched.append((name, cmd, ram, cpu, sl)) or 7,
    )
    return verdicts, launched


def test_run_go_launches_with_the_estimates(flow):
    verdicts, launched = flow
    verdicts.append(GO)
    rc = cli.main(["run", "--name", "j", "--ram", "1", "--cpu", "50", "--", "make", "-j4"])
    assert rc == 7 and launched == [("j", ["make", "-j4"], 2**30, 50.0, None)]


def test_run_not_go_does_not_launch(flow):
    verdicts, launched = flow
    verdicts.append(NO)
    assert cli.main(["run", "--name", "j", "--ram", "1", "--cpu", "50", "--", "true"]) == 2
    assert launched == []


def test_run_wait_exits_3_without_a_deadline(flow):
    verdicts, launched = flow
    verdicts.append(WAIT)
    assert cli.main(["run", "--name", "j", "--ram", "1", "--cpu", "50", "--", "true"]) == 3
    assert launched == []


def test_run_wait_until_fits_polls_then_launches(flow):
    verdicts, launched = flow
    verdicts.extend([WAIT, WAIT, GO])
    argv = [
        "run",
        "--name",
        "j",
        "--ram",
        "1",
        "--cpu",
        "50",
        "--wait-until-fits",
        "5",
        "--",
        "true",
    ]
    assert cli.main(argv) == 7 and len(launched) == 1 and verdicts == []


def test_run_requires_a_command(flow):
    assert cli.main(["run", "--name", "j", "--ram", "1", "--cpu", "50"]) == cli.EXIT_USAGE


def test_run_wait_gives_up_at_the_deadline(flow):
    verdicts, launched = flow
    verdicts.extend([WAIT] * 50)
    argv = [
        "run",
        "--name",
        "j",
        "--ram",
        "1",
        "--cpu",
        "50",
        "--wait-until-fits",
        "0",
        "--",
        "true",
    ]
    assert cli.main(argv) == 3 and launched == []


def test_run_rejects_a_ram_estimate_too_small_to_hold_a_process(flow):
    assert cli.main(["run", "--name", "j", "--ram", "0", "--cpu", "50", "--", "true"]) == 64


def test_run_refused_scope_is_a_usage_error(flow, monkeypatch, capsys):
    verdicts, _ = flow
    verdicts.append(GO)

    def refuse(*a):
        raise run.ProbeRefused("Value specified in MemoryMax is out of range")

    monkeypatch.setattr(run, "launch", refuse)
    assert cli.main(["run", "--name", "j", "--ram", "1", "--cpu", "50", "--", "true"]) == 64
    assert "systemd refused the job's scope: Value specified" in capsys.readouterr().err


def test_run_wait_re_reads_the_host_each_poll(flow, monkeypatch):
    verdicts, launched = flow
    verdicts.extend([WAIT, WAIT, GO])
    cleared = []
    monkeypatch.setattr(cli.host_memory_once, "cache_clear", lambda: cleared.append(1))
    argv = [
        "run",
        "--name",
        "j",
        "--ram",
        "1",
        "--cpu",
        "50",
        "--wait-until-fits",
        "5",
        "--",
        "true",
    ]
    assert cli.main(argv) == 7 and cleared == [1, 1]


def test_uncapped_limit_is_the_data_segment_not_address_space(monkeypatch):
    # MEASURED in review: an address-space limit at typical estimates kills the JVM,
    # node and OpenBLAS at start, because they reserve ranges they never touch.
    calls = []
    monkeypatch.setattr(run.os, "nice", lambda n: calls.append(("nice", n)))
    monkeypatch.setattr(run.resource, "setrlimit", lambda which, lim: calls.append((which, lim)))
    run._uncapped_limits(2**30)()
    assert calls == [("nice", 19), (run.resource.RLIMIT_DATA, (2**30, 2**30))]


class _SignallingProc:
    """A launched job whose wait() delivers signals to this process first, the way a
    user's Ctrl-C or a supervisor's SIGTERM would arrive while `run` waits."""

    pid = 4242  # explicit: a mock pid of 1 would make killpg hit every process

    def __init__(self, signals):
        self.signals = signals

    def wait(self):
        import signal as _signal

        for sig in self.signals:
            os.kill(os.getpid(), sig)
        return -_signal.SIGTERM


def _launch_with(monkeypatch, caps, signals):
    import signal as _signal

    killed, stopped = [], []
    monkeypatch.setattr(run, "choose_properties", lambda *a: caps)
    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, **kw: _SignallingProc(signals))
    monkeypatch.setattr(
        run, "kill_group", lambda pgid, sig=_signal.SIGTERM: killed.append((pgid, sig))
    )
    monkeypatch.setattr(run, "stop_scope", lambda unit, *a: stopped.append(unit))
    rc = run.launch("j", ["true"], 2**30, 100, lambda: None)
    return rc, killed, stopped


def test_a_stop_reaches_the_launch_before_the_scope_exists(monkeypatch):
    # Before systemd registers the scope the job is still the systemd-run client:
    # stopping only the (not yet existing) unit would let the job start anyway.
    import signal as _signal

    caps = run.Caps(run.scope_properties(2**30, 100))
    rc, killed, stopped = _launch_with(monkeypatch, caps, [_signal.SIGTERM])
    assert killed == [(4242, _signal.SIGTERM)] and len(stopped) == 1
    assert rc == 128 + _signal.SIGTERM


def test_a_repeated_signal_escalates_to_sigkill(monkeypatch):
    import signal as _signal

    rc, killed, _ = _launch_with(monkeypatch, None, [_signal.SIGTERM, _signal.SIGINT])
    assert killed == [(4242, _signal.SIGTERM), (4242, _signal.SIGKILL)]


def test_an_unenforced_cap_is_announced(monkeypatch, capsys):
    caps = run.Caps(run.scope_properties(2**30, 100), ("memory",), ("cpu",))
    _launch_with(monkeypatch, caps, [])
    err = capsys.readouterr().err
    assert "the memory cap is NOT enforced here" in err
    assert "could not verify the cpu cap" in err


def test_watchdog_times_the_box_over_the_line_whatever_the_reason():
    # Memory then a disk is still the box over the line: one continuous timer, so
    # alternating reasons can never postpone the stop forever (review, MEASURED).
    over = iter(["mem", "disk", "mem", "disk", "mem", "disk", "mem"])
    stops = []
    dog = run.Watchdog(lambda: next(over), lambda: stops.append(1), grace=60)
    assert [dog.tick(t) for t in (0, 10, 20, 30, 40, 50)] == [False] * 6
    assert dog.tick(60) is True and dog.fired == "mem for 60s" and stops == [1]


def test_run_rejects_abbreviated_options(flow):
    # `--na` would mean `--name` under argparse's default prefix matching.
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--na", "t", "--ram", "1", "--cpu", "50", "--", "true"])
    assert exc.value.code == cli.EXIT_USAGE


def test_run_rejects_a_non_finite_wait(flow):
    argv = [
        "run",
        "--name",
        "j",
        "--ram",
        "1",
        "--cpu",
        "50",
        "--wait-until-fits",
        "nan",
        "--",
        "true",
    ]
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == cli.EXIT_USAGE


@pytest.mark.parametrize(
    ("caps", "limited"),
    [
        (run.Caps(["MemoryMax=1"]), False),
        (run.Caps(["MemoryMax=1"], ("memory",)), True),
        (run.Caps(["MemoryMax=1"], (), ("cpu",)), True),
    ],
)
def test_a_cap_the_kernel_is_not_applying_gets_the_fallback_limits(monkeypatch, caps, limited):
    seen = {}
    monkeypatch.setattr(run, "choose_properties", lambda *a: caps)
    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, **kw: seen.update(kw) or _Proc(argv))
    run.launch("j", ["true"], 2**30, 100, lambda: None)
    assert (seen.get("preexec_fn") is not None) is limited
