"""Strict execution evidence is scoped, complete, and separate from payload IO."""

import fcntl
import json
import os
import runpy
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/lib/code_intel_execution.py"
UNIT = "code-intel-0123456789ab-cbm-123"
CAP = 8 * 1024**3


@pytest.fixture
def evidence(monkeypatch):
    namespace = runpy.run_path(str(SCRIPT))
    actual_status = namespace["_payload_status"]
    actual_child = subprocess.Popen

    def status(child):
        # Mock payloads model state; real subprocesses keep the real wait path.
        return actual_status(child) if isinstance(child, actual_child) else child.poll()

    monkeypatch.setitem(namespace["observe"].__globals__, "_payload_status", status)
    return namespace


@pytest.fixture
def scope(evidence, tmp_path):
    proc = tmp_path / "proc"
    root = tmp_path / "cgroup"
    leaf = root / "genesis-workload.slice" / (UNIT + ".scope")
    leaf.mkdir(parents=True)
    membership = f"0::/genesis-workload.slice/{UNIT}.scope\n"
    (proc / "self/task" / str(os.getpid())).mkdir(parents=True)
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("00000000-0000-0000-0000-000000000001")
    (proc / "self/stat").write_text(
        f"{os.getpid()} (owned reporter) " + " ".join(["S"] + ["0"] * 18 + ["1000"])
    )
    for name, value in {
        "cgroup": membership,
        "mountinfo": f"1 0 0:1 / {root} rw - cgroup2 cgroup rw\n",
        "oom_score_adj": "0\n",
    }.items():
        (proc / "self" / name).write_text(value)
    (proc / "self/task" / str(os.getpid()) / "cgroup").write_text(membership)
    for name, value in {
        "memory.max": str(CAP),
        "memory.swap.max": "0",
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
        "memory.events.local": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
        "pids.current": "1",
        "cgroup.procs": str(os.getpid()),
    }.items():
        (leaf / name).write_text(value)
    return evidence["ScopeProbe"](UNIT, CAP, proc=proc), leaf, proc


def test_scope_snapshot_and_actual_finality(scope):
    probe, leaf, proc = scope
    facts = probe.capture()
    assert facts["version"] == 2 and facts["memory_max"] == CAP and facts["swap_max"] == 0
    assert facts["leaf"]["inode"] == leaf.stat().st_ino
    assert facts["events"]["oom_kill"] == facts["local_events"]["oom_kill"] == 0
    assert probe.finality(facts)["pids_current"] == 1


@pytest.mark.parametrize(
    "fault",
    [
        "v1",
        "local-events",
        "cap",
        "swap",
        "missing-kill",
        "duplicate",
        "missing-pids",
        "maximum-reporter",
    ],
)
def test_unprovable_before_payload_refuses(scope, fault, monkeypatch, evidence):
    probe, leaf, proc = scope
    if fault == "v1":
        (proc / "self/cgroup").write_text("3:memory:/owned\n")
    elif fault == "local-events":
        path = proc / "self/mountinfo"
        path.write_text(path.read_text().replace("cgroup rw", "cgroup rw,memory_localevents"))
    elif fault in ["cap", "swap", "missing-pids"]:
        name, value = {
            "cap": ("memory.max", "max"),
            "swap": ("memory.swap.max", "1"),
            "missing-pids": ("pids.current", "invalid"),
        }[fault]
        (leaf / name).write_text(value)
    elif fault in ["missing-kill", "duplicate"]:
        path = leaf / "memory.events"
        path.write_text(
            path.read_text().replace("oom_kill 0\n", "")
            if fault == "missing-kill"
            else path.read_text() + "oom_kill 0\n"
        )
    else:
        (proc / "self/oom_score_adj").write_text("1000")
    starts = []
    monkeypatch.setattr(evidence["subprocess"], "Popen", lambda *a, **k: starts.append(a))
    report = evidence["observe"](probe, ["owned-payload"], seconds=1)
    assert report["outcome"] == "refused" and report["payload_started"] is False
    assert starts == []


def test_foreground_zero_does_not_hide_nested_process(scope):
    probe, leaf, proc = scope
    child = leaf / "child"
    child.mkdir()
    (child / "cgroup.procs").write_text("424242")
    (leaf / "pids.current").write_text("2")
    assert probe.finality(probe.capture()) is None


@pytest.mark.parametrize("depth", [1, 3])
def test_recursive_scan_finds_a_nested_task_the_count_does_not_show(scope, depth):
    probe, leaf, proc = scope
    nested = leaf
    for level in range(depth):
        nested = nested / f"child-{level}"
        nested.mkdir()
        (nested / "cgroup.procs").write_text("")
    (nested / "cgroup.procs").write_text("424242")
    assert (leaf / "pids.current").read_text() == "1"
    assert probe.finality(probe.capture()) is None


def test_atomic_count_is_authority_even_if_recursive_scan_misses_task(scope):
    probe, leaf, proc = scope
    (leaf / "pids.current").write_text("2")
    assert probe.finality(probe.capture()) is None


def test_deadline_after_start_reports_uncertainty_without_claiming_cleanup(
    scope, evidence, monkeypatch
):
    probe, leaf, proc = scope
    calls = []

    class Payload:
        def poll(self):
            calls.append("observed")
            return None

    monkeypatch.setattr(evidence["subprocess"], "Popen", lambda *a, **k: Payload())
    clock = [100.0]
    monkeypatch.setitem(
        evidence["observe"].__globals__,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    report = evidence["observe"](probe, ["owned-payload"], seconds=0.05)
    assert report["outcome"] == "uncertain" and report["payload_started"] is True
    assert report["payload_returncode"] is None and report["error"] == "deadline_reached"
    assert calls


def test_complete_report_size_is_checked_before_spawn(scope, evidence, monkeypatch):
    probe, leaf, proc = scope
    starts = []
    monkeypatch.setattr(evidence["subprocess"], "Popen", lambda *a, **k: starts.append(a))
    report = evidence["observe"](probe, ["owned-payload"], seconds=1, report_limit=100)
    assert report["outcome"] == "refused" and report["payload_started"] is False
    assert starts == []


def _encoded_sizes(evidence, monkeypatch):
    globals_ = evidence["observe"].__globals__
    actual, sizes = globals_["_encode"], []

    def encode(report):
        data = actual(report)
        sizes.append(len(data))
        return data

    monkeypatch.setitem(globals_, "_encode", encode)
    return actual, sizes


def _payload_returning(rc, change=None):
    class Payload:
        def poll(self):
            return rc

    def start(*args, **kwargs):
        if change:
            change()
        return Payload()

    return start


def test_size_check_uses_the_worst_case_not_the_actual_report(scope, evidence, monkeypatch):
    probe, leaf, proc = scope
    encode, sizes = _encoded_sizes(evidence, monkeypatch)
    monkeypatch.setattr(evidence["subprocess"], "Popen", _payload_returning(0))
    result = evidence["observe"](probe, ["owned-payload"], seconds=1)
    assert result["outcome"] == "complete"
    worst, actual = sizes[0], len(encode(result))
    assert actual < worst
    starts = []
    monkeypatch.setattr(evidence["subprocess"], "Popen", lambda *a, **k: starts.append(a))
    limited = evidence["observe"](
        probe, ["owned-payload"], seconds=1, report_limit=(actual + worst) // 2
    )
    assert limited["outcome"] == "refused" and starts == []


@pytest.mark.parametrize("fault", ["optional-population", "identity", "ancestor-events"])
def test_final_sample_outside_the_checked_shape_is_dropped_and_stays_uncertain(
    scope, evidence, monkeypatch, fault
):
    probe, leaf, proc = scope
    encode, sizes = _encoded_sizes(evidence, monkeypatch)
    root = leaf.parent.parent
    if fault == "ancestor-events":
        (leaf.parent / "memory.events").mkdir()

    def change():
        if fault == "optional-population":
            path = leaf / "memory.events"
            path.write_text(path.read_text() + "oom_group_kill 0\n")
        elif fault == "identity":
            # Accepted by capture (still below maximum), but the scope's
            # identity differs, so the sample would encode uncompressed.
            (proc / "self/oom_score_adj").write_text("5\n")
        else:
            (leaf.parent / "memory.events").rmdir()
            (leaf.parent / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")

    monkeypatch.setattr(evidence["subprocess"], "Popen", _payload_returning(0, change))
    result = evidence["observe"](probe, ["owned-payload"], seconds=0.3)
    assert root.exists()
    assert result["outcome"] == "uncertain" and result["after"] is None
    assert len(encode(result)) <= sizes[0]


def test_reporter_thread_growth_keeps_the_run_uncertain(scope, evidence, monkeypatch):
    probe, leaf, proc = scope
    tid = os.getpid() + 1

    def grow():
        task = proc / "self/task" / str(tid)
        task.mkdir()
        (task / "cgroup").write_text((proc / "self/cgroup").read_text())
        (leaf / "pids.current").write_text("2")
        (leaf / "cgroup.procs").write_text(str(os.getpid()))

    monkeypatch.setattr(evidence["subprocess"], "Popen", _payload_returning(0, grow))
    result = evidence["observe"](probe, ["owned-payload"], seconds=0.3)
    assert result["outcome"] == "uncertain" and result["after"] is None


def test_ordinary_systemd_hierarchy_fits_without_losing_shared_facts(scope, evidence, monkeypatch):
    probe, leaf, proc = scope
    before = probe.capture()
    root = Path("/sys/fs/cgroup")
    target = (
        root
        / "user.slice/user-1000.slice/user@1000.service/genesis-workload.slice"
        / (UNIT + ".scope")
    )
    before["leaf"]["path"], before["root"]["path"] = str(target), str(root)
    before["membership"] = "0::/" + str(target.relative_to(root)) + "\n"
    before["events"]["oom_group_kill"] = before["local_events"]["oom_group_kill"] = 0
    ancestors, parent = [], target.parent
    while True:
        ancestors.append(
            {
                "identity": {**before["leaf"], "path": str(parent)},
                "events": dict(before["events"]) if parent != root else None,
                "error": None if parent != root else "ancestor_events_unavailable",
            }
        )
        if parent == root:
            break
        parent = parent.parent
    before["ancestors"] = ancestors
    finality = {"threads": [os.getpid()], "processes": [os.getpid()], "pids_current": 1}
    fake = SimpleNamespace(capture=lambda: before, finality=lambda snapshot: finality)
    monkeypatch.setattr(
        evidence["subprocess"], "Popen", lambda *a, **kw: SimpleNamespace(poll=lambda: 0)
    )
    report = evidence["observe"](fake, ["owned"], seconds=1)
    assert report["outcome"] == "complete" and report["payload_started"] is True
    wire = evidence["_encode"](report)
    assert len(wire) <= 4096 and json.loads(wire)["after"]["same_scope_as_before"] is True
    read_fd, write_fd = os.pipe()
    try:
        evidence["write_report"](write_fd, report)
        os.close(write_fd)
        write_fd = None
        assert evidence["read_report"](read_fd) == report
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.parametrize(
    "fault", ["inode", "counter-reset", "optional-population", "cap", "membership"]
)
def test_terminal_changes_cannot_be_complete(scope, evidence, monkeypatch, fault):
    probe, leaf, proc = scope
    if fault == "counter-reset":
        (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 2\n")

    class Payload:
        def poll(self):
            return 0

    def start(*args, **kwargs):
        assert kwargs["close_fds"] is True and not kwargs.get("pass_fds")
        if fault == "inode":
            leaf.rename(leaf.with_name("retained-scope"))
            leaf.mkdir()
        elif fault == "counter-reset":
            (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        elif fault == "optional-population":
            path = leaf / "memory.events"
            path.write_text(path.read_text() + "oom_group_kill 0\n")
        elif fault == "cap":
            (leaf / "memory.max").write_text(str(CAP // 2))
        else:
            (proc / "self/cgroup").write_text("0::/elsewhere\n")
        return Payload()

    monkeypatch.setattr(evidence["subprocess"], "Popen", start)
    result = evidence["observe"](probe, ["owned-payload"], seconds=1)
    assert result["outcome"] == "uncertain" and result["payload_started"] is True


@pytest.mark.parametrize("rc", [0, 7, -9, 125])
def test_observer_retains_actual_payload_status_and_kill_facts(scope, evidence, monkeypatch, rc):
    probe, leaf, proc = scope

    class Payload:
        def poll(self):
            return rc

    def start(*args, **kwargs):
        path = leaf / "memory.events"
        path.write_text(path.read_text().replace("oom_kill 0", "oom_kill 1"))
        return Payload()

    monkeypatch.setattr(evidence["subprocess"], "Popen", start)
    result = evidence["observe"](probe, ["owned-payload"], seconds=1)
    assert result["outcome"] == "complete" and result["payload_returncode"] == rc
    assert result["after"]["events"]["oom_kill"] - result["before"]["events"]["oom_kill"] == 1


def test_pipe_transport_is_separate_and_exact(evidence):
    read_fd, write_fd = os.pipe()
    try:
        report = {
            "version": 1,
            "outcome": "refused",
            "payload_started": False,
            "payload_returncode": None,
            "before": None,
            "after": None,
            "finality": None,
            "error": "proof_unavailable",
        }
        evidence["write_report"](write_fd, report)
        os.close(write_fd)
        write_fd = None
        assert evidence["read_report"](read_fd) == report
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.parametrize(
    "payload", [b"", b"{}\n", b'{"version":1,"version":1}\n', b"{}\n{}\n", b"x" * 4097]
)
def test_invalid_or_extra_report_is_not_terminal_proof(evidence, payload):
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = None
        with pytest.raises(ValueError):
            evidence["read_report"](read_fd)
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.parametrize("report_fd", [3, 80])
@pytest.mark.parametrize("death", ["none", "before-write", "after-write"])
def test_real_payload_cannot_inherit_or_forge_report_channel(
    scope, evidence, tmp_path, report_fd, death
):
    probe, leaf, proc = scope
    accepted = tmp_path / "accepted executable bytes"
    accepted.write_bytes(b"owned accepted bytes")
    read_fd, write_fd = os.pipe()
    payload = (
        "import os,sys; "
        "fd=os.open(sys.argv[1],os.O_RDONLY); "
        'assert fd==3; assert os.read(fd,100)==b"owned accepted bytes"; '
        'print("stdout is not a report: {\\"outcome\\":\\"complete\\"}")\n'
        'try:\n os.write(int(sys.argv[2]),b"forged report\\n")\n'
        "except OSError:\n pass\n"
        'else:\n raise AssertionError("payload inherited report pipe")\n'
    )
    supervisor = (
        "import os,runpy,sys; from pathlib import Path; "
        "source,proc,leaf,unit,cap,write,report_fd,accepted,payload,death=sys.argv[1:]; "
        "proc=Path(proc); leaf=Path(leaf); report_fd=int(report_fd); write=int(write); "
        "os.dup2(write,report_fd); "
        "os.close(write) if write!=report_fd else None; "
        '(leaf/"cgroup.procs").write_text(str(os.getpid())); '
        'task=next((proc/"self/task").iterdir()); task.rename(task.with_name(str(os.getpid()))); '
        '(proc/"self/stat").write_text(Path("/proc/self/stat").read_text()); '
        "ns=runpy.run_path(source); "
        'probe=ns["ScopeProbe"](unit,int(cap),proc=proc); '
        'limit=ns["pipe_limit"](report_fd); '
        'result=ns["observe"](probe,[sys.executable,"-I","-c",payload,accepted,str(report_fd)],seconds=5,report_limit=limit); '
        'os.kill(os.getpid(),9) if death=="before-write" else None; '
        'ns["write_report"](report_fd,result); '
        'os.kill(os.getpid(),9) if death=="after-write" else None'
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                supervisor,
                str(SCRIPT),
                str(proc),
                str(leaf),
                UNIT,
                str(CAP),
                str(write_fd),
                str(report_fd),
                str(accepted),
                payload,
                death,
            ],
            pass_fds=(write_fd,),
            capture_output=True,
            text=True,
        )
        assert result.returncode == (0 if death == "none" else -signal.SIGKILL), result.stderr
        os.close(write_fd)
        write_fd = None
        if death == "before-write":
            with pytest.raises(ValueError, match="missing or multiple"):
                evidence["read_report"](read_fd)
            return
        report = evidence["read_report"](read_fd)
        assert report["outcome"] == "complete" and report["payload_returncode"] == 0, report
        assert "stdout is not a report" in result.stdout
        assert (
            report["before"]["reporter"]["pid"] > 0
            and report["before"]["reporter"]["start_ticks"] > 0
        )
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.parametrize("standard", [0, 1, 2])
def test_report_pipe_stdio_alias_refuses_in_owned_child(evidence, standard):
    read_fd, write_fd = os.pipe()
    program = (
        "import os,runpy,sys; ns=runpy.run_path(sys.argv[1]); "
        "fd=int(sys.argv[2]); os.dup2(fd,int(sys.argv[3])); "
        "os.dup2(fd,80);\n"
        'try:\n ns["pipe_limit"](80)\n'
        "except ValueError:\n pass\n"
        'else:\n raise AssertionError("stdio alias accepted")\n'
    )
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", program, str(SCRIPT), str(write_fd), str(standard)],
            pass_fds=(write_fd,),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        os.close(write_fd)
        write_fd = None
        assert os.read(read_fd, 4096) == b""
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_full_pipe_is_visible_delivery_failure(evidence):
    read_fd, write_fd = os.pipe()
    try:
        size = fcntl.fcntl(write_fd, fcntl.F_GETPIPE_SZ)
        os.set_blocking(write_fd, False)
        assert os.write(write_fd, b"x" * size) == size
        with pytest.raises(BlockingIOError):
            evidence["write_report"](write_fd, {"bounded": "report"})
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_short_reads_are_reassembled_through_eof(evidence, monkeypatch):
    read_fd, write_fd = os.pipe()
    report = {
        "version": 1,
        "outcome": "refused",
        "payload_started": False,
        "payload_returncode": None,
        "before": None,
        "after": None,
        "finality": None,
        "error": "proof_unavailable",
    }
    try:
        evidence["write_report"](write_fd, report)
        os.close(write_fd)
        write_fd = None
        original = os.read
        with monkeypatch.context() as patch:
            patch.setitem(
                evidence["read_report"].__globals__,
                "os",
                SimpleNamespace(
                    read=lambda fd, size: original(fd, min(size, 3)),
                ),
            )
            assert evidence["read_report"](read_fd) == report
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_ancestor_kills_and_high_max_are_separate_from_owned_kills(scope, evidence, monkeypatch):
    probe, leaf, proc = scope
    ancestor = leaf.parent / "memory.events"
    ancestor.write_text((leaf / "memory.events").read_text())

    def start(*args, **kwargs):
        ancestor.write_text(ancestor.read_text().replace("oom_kill 0", "oom_kill 1"))
        local = leaf / "memory.events"
        local.write_text(local.read_text().replace("high 0", "high 1").replace("max 0", "max 1"))
        return SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(evidence["subprocess"], "Popen", start)
    report = evidence["observe"](probe, ["owned"], seconds=1)
    assert report["outcome"] == "complete" and report["after"]["events"]["oom_kill"] == 0
    assert report["after"]["events"]["high"] == report["after"]["events"]["max"] == 1
    assert report["after"]["ancestors"][0]["events"]["oom_kill"] == 1


@pytest.mark.parametrize(
    "fault",
    [
        "missing-fact",
        "boolean-counter",
        "boolean-process",
        "wrong-task-count",
        "missing-reference",
        "wrong-reference-size",
    ],
)
def test_malformed_complete_records_are_rejected(scope, evidence, fault):
    probe, leaf, proc = scope
    facts = probe.capture()
    report = {
        "version": 1,
        "outcome": "complete",
        "payload_started": True,
        "payload_returncode": 0,
        "before": facts,
        "after": facts,
        "finality": probe.finality(facts),
        "error": None,
    }
    wire = json.loads(evidence["_encode"](report))
    if fault == "missing-fact":
        del wire["before"]["leaf"]
    elif fault == "boolean-counter":
        wire["before"]["events"]["oom_kill"] = False
    elif fault == "boolean-process":
        wire["before"]["reporter"]["pid"] = 1
        wire["finality"]["threads"] = [1]
        wire["finality"]["processes"] = [True]
    elif fault == "wrong-task-count":
        wire["finality"]["pids_current"] = 2
    elif fault == "missing-reference":
        wire["before"] = None
    else:
        wire["after"]["ancestors"] = []
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, (json.dumps(wire) + "\n").encode())
        os.close(write_fd)
        write_fd = None
        with pytest.raises(ValueError):
            evidence["read_report"](read_fd)
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.parametrize("cap", [str(CAP), "invalid"])
def test_actual_cli_outside_expected_scope_never_starts_payload(evidence, tmp_path, cap):
    marker = tmp_path / "payload-must-not-start"
    read_fd, write_fd = os.pipe()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(SCRIPT),
                "--report-fd",
                str(write_fd),
                "--unit",
                UNIT,
                "--cap-bytes",
                cap,
                "--seconds",
                "1",
                "--",
                sys.executable,
                "-I",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                str(marker),
            ],
            pass_fds=(write_fd,),
            capture_output=True,
            text=True,
        )
        os.close(write_fd)
        write_fd = None
        assert not marker.exists() and not result.stdout
        if cap == "invalid":
            assert result.returncode == 125 and "transport failed" in result.stderr
            with pytest.raises(ValueError):
                evidence["read_report"](read_fd)
        else:
            assert result.returncode == 0  # Delivery, not native success.
            report = evidence["read_report"](read_fd)
            assert report["outcome"] == "refused" and report["payload_started"] is False
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_broken_pipe_is_not_a_successful_delivery(evidence):
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        with pytest.raises(BrokenPipeError):
            evidence["write_report"](write_fd, {"owned": "facts"})
    finally:
        os.close(write_fd)


@pytest.mark.parametrize("ending", ["normal", "oom", "survives"])
def test_foreground_zero_waits_for_nested_descendant(scope, evidence, monkeypatch, ending):
    probe, leaf, proc = scope
    clock, starts = [100.0], []
    nested = leaf / "child/grandchild"

    def start(*args, **kwargs):
        starts.append(True)
        nested.mkdir(parents=True)
        (nested.parent / "cgroup.procs").write_text("")
        (nested / "cgroup.procs").write_text("424242")
        (leaf / "pids.current").write_text("2")
        return SimpleNamespace(poll=lambda: 0)

    def advance(seconds):
        clock[0] += seconds
        if ending != "survives" and clock[0] >= 100.1:
            (nested / "cgroup.procs").write_text("")
            (leaf / "pids.current").write_text("1")
            if ending == "oom":
                events = leaf / "memory.events"
                events.write_text(events.read_text().replace("oom_kill 0", "oom_kill 1"))

    monkeypatch.setattr(evidence["subprocess"], "Popen", start)
    monkeypatch.setitem(
        evidence["observe"].__globals__,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=advance,
        ),
    )
    report = evidence["observe"](probe, ["owned"], seconds=0.2)
    assert starts == [True] and report["payload_returncode"] == 0 and clock[0] >= 100.1
    if ending == "survives":
        assert report["outcome"] == "uncertain" and report["error"] == "deadline_reached"
        assert (nested / "cgroup.procs").read_text() == "424242"
    else:
        assert report["outcome"] == "complete"
        assert report["after"]["events"]["oom_kill"] == (1 if ending == "oom" else 0)


def test_own_threads_are_allowed_only_with_verified_leaf_membership(scope):
    probe, leaf, proc = scope
    extra = proc / "self/task" / str(os.getpid() + 1)
    extra.mkdir()
    (extra / "cgroup").write_text((proc / "self/cgroup").read_text())
    (leaf / "pids.current").write_text("2")
    facts = probe.capture()
    assert probe.finality(facts)["pids_current"] == 2
    (extra / "cgroup").write_text("0::/outside\n")
    with pytest.raises(ValueError, match="thread outside"):
        probe.finality(facts)


@pytest.mark.parametrize("mode", ["zero", "nonzero", "signal", "ignored", "competing-reaper"])
def test_real_child_status_is_not_invented_when_wait_status_is_lost(scope, tmp_path, mode):
    probe, leaf, proc = scope
    marker = tmp_path / "actual-payload"
    payload = (
        "from pathlib import Path; import os,signal,sys; "
        "Path(sys.argv[1]).write_text('started'); "
        "os.kill(os.getpid(),signal.SIGTERM) if sys.argv[2]=='signal' else None; "
        "sys.exit(0 if sys.argv[2]=='zero' else 23)"
    )
    supervisor = (
        "import json,os,runpy,signal,sys; from pathlib import Path; "
        "source,proc,leaf,unit,cap,marker,mode,payload=sys.argv[1:]; "
        "proc=Path(proc); leaf=Path(leaf); "
        '(leaf/"cgroup.procs").write_text(str(os.getpid())); '
        'task=next((proc/"self/task").iterdir()); task.rename(task.with_name(str(os.getpid()))); '
        '(proc/"self/stat").write_text(Path("/proc/self/stat").read_text()); '
        'ns=runpy.run_path(source); probe=ns["ScopeProbe"](unit,int(cap),proc=proc); '
        'signal.signal(signal.SIGCHLD,signal.SIG_IGN) if mode=="ignored" else None; '
        'real_spawn=ns["subprocess"].Popen; '
        "\ndef reaped_spawn(*args,**kwargs):\n"
        " child=real_spawn(*args,**kwargs)\n"
        " pid,status=os.waitpid(child.pid,0)\n"
        " assert pid==child.pid and os.waitstatus_to_exitcode(status)==23\n"
        " return child\n"
        'ns["subprocess"].Popen=reaped_spawn if mode=="competing-reaper" else real_spawn\n'
        'report=ns["observe"](probe,[sys.executable,"-I","-c",payload,marker,mode],seconds=2)\n'
        "print(json.dumps(report))\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            supervisor,
            str(SCRIPT),
            str(proc),
            str(leaf),
            UNIT,
            str(CAP),
            str(marker),
            mode,
            payload,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    if mode == "ignored":
        assert report["outcome"] == "refused" and report["payload_started"] is False
        assert not marker.exists()
    elif mode == "competing-reaper":
        assert report["outcome"] == "uncertain" and report["payload_started"] is True
        assert report["payload_returncode"] is None and marker.read_text() == "started"
    else:
        assert report["outcome"] == "complete" and marker.read_text() == "started"
        assert (
            report["payload_returncode"]
            == {"zero": 0, "nonzero": 23, "signal": -signal.SIGTERM}[mode]
        )
