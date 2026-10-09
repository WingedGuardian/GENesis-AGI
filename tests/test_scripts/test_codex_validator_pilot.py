"""Fixed pilot admission, source proof, case census and receipt lifecycle."""

import copy
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return private_module("codex_pilot_under_test", ROOT / "scripts/codex_validator_pilot.py")


def _xml(pilot, recipe="queue_snapshot"):
    root = Element("testsuites")
    nodes = pilot.RECIPES[recipe][0]
    suite = SubElement(
        root, "testsuite", tests=str(sum(nodes.values())), errors="0", failures="0", skipped="0"
    )
    for node, count in nodes.items():
        file, name = node.split("::")
        for index in range(count):
            SubElement(
                suite,
                "testcase",
                classname=file.removesuffix(".py").replace("/", "."),
                name=name if count == 1 else f"{name}[exc{index}]",
            )
    return root


@pytest.fixture
def configured(pilot, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    trusted = workspace / ".codex"
    trusted.mkdir(mode=0o700)
    (trusted / "receipts").mkdir(mode=0o700)
    (trusted / "operation.lock").touch(mode=0o600)
    config = {
        "version": 1,
        "runtime_commit": "a" * 40,
        "ledger": str(tmp_path / "ledger.db"),
        "rows": [
            {
                "pr": 7,
                "repo": "owner/repo",
                "merge_commit": "b" * 40,
                "recipe": "queue_snapshot",
                "intent": "fixture intent",
                "sources": pilot.source_hashes(ROOT, "queue_snapshot"),
            }
        ],
    }
    _config_write(workspace, config)
    return workspace, config


def _config_write(workspace, config):
    path = workspace / ".codex/pilot.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)


@pytest.mark.parametrize(
    "recipe, count", [("peer_availability", 9), ("exhaustion", 5), ("queue_snapshot", 2)]
)
def test_exact_fixture_census(pilot, recipe, count):
    cases = pilot.measured_cases(tostring(_xml(pilot, recipe)), recipe)
    assert len(cases) == count and cases == sorted(set(cases))


@pytest.mark.parametrize(
    "fault",
    ["empty", "missing", "duplicate", "foreign", "failed", "skipped", "error", "suite_error"],
)
def test_case_census_rejects_incomplete_and_false_success(pilot, fault):
    root = _xml(pilot)
    suite = root[0]
    if fault == "empty":
        suite.clear()
    elif fault == "missing":
        suite.remove(suite[0])
    elif fault == "duplicate":
        suite.append(copy.deepcopy(suite[0]))
    elif fault == "foreign":
        suite[0].set("classname", "foreign.test")
    elif fault == "suite_error":
        suite.set("errors", "1")
    else:
        SubElement(suite[0], {"failed": "failure"}.get(fault, fault))
    with pytest.raises(ValueError):
        pilot.measured_cases(tostring(root), "queue_snapshot")


@pytest.mark.parametrize(
    "declaration", [b"<!DOCTYPE testsuites>", b'<!DOCTYPE testsuites [<!ENTITY x "payload">]>']
)
def test_case_parser_refuses_declarations(pilot, declaration):
    with pytest.raises(ValueError, match="unsupported declarations"):
        pilot.measured_cases(declaration + tostring(_xml(pilot)), "queue_snapshot")


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("runtime_commit", "bad"),
        ("ledger", "relative.db"),
        ("rows", []),
        ("rows", [None]),
    ],
)
def test_config_refuses_malformed_top_level(pilot, configured, field, value):
    workspace, config = configured
    config[field] = value
    _config_write(workspace, config)
    with pytest.raises(ValueError):
        pilot.configuration(workspace)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pr", True),
        ("pr", 0),
        ("repo", "not-a-slug"),
        ("merge_commit", "bad"),
        ("recipe", "shell"),
        ("intent", ""),
        ("intent", "x" * 8001),
        ("sources", {}),
    ],
)
def test_config_refuses_malformed_row(pilot, configured, field, value):
    workspace, config = configured
    config["rows"][0][field] = value
    _config_write(workspace, config)
    with pytest.raises(ValueError):
        pilot.configuration(workspace)


def test_config_refuses_duplicates_unknown_keys_and_bad_digests(pilot, configured):
    workspace, original = configured
    for mutate in (
        lambda c: c["rows"].append(copy.deepcopy(c["rows"][0])),
        lambda c: c.update(argv=["shell"]),
        lambda c: c["rows"][0].update(extra=True),
        lambda c: c["rows"][0]["sources"].update({next(iter(c["rows"][0]["sources"])): "g" * 64}),
    ):
        config = copy.deepcopy(original)
        mutate(config)
        _config_write(workspace, config)
        with pytest.raises(ValueError):
            pilot.configuration(workspace)


def test_packet_excludes_ledger_and_declares_fixture_limits(pilot, configured):
    workspace, config = configured
    packet = pilot.packet(workspace)
    assert "ledger" not in packet and packet["runtime_commit"] == config["runtime_commit"]
    assert packet["rows"][0]["scope_limit"] == pilot.RECIPES["queue_snapshot"][2]


def test_source_mismatch_refuses_before_status(pilot, configured, monkeypatch):
    _, config = configured
    row = config["rows"][0]
    row["sources"][next(iter(row["sources"]))] = "0" * 64
    monkeypatch.setattr(
        pilot, "observe", lambda *a, **k: pytest.fail("mismatched source entered status")
    )
    with pytest.raises(ValueError, match="source changed"):
        pilot.bound_state(ROOT, config, row)


@pytest.mark.parametrize(
    "state",
    [
        {"established": False},
        {"established": True, "head": "c" * 40, "serving": "a" * 40},
        {"established": True, "head": "a" * 40, "serving": "c" * 40},
    ],
)
def test_unknown_or_other_deployment_refuses(pilot, configured, monkeypatch, state):
    _, config = configured
    monkeypatch.setattr(pilot, "observe", lambda *a, **k: state)
    with pytest.raises(ValueError, match="deployment unavailable"):
        pilot.bound_state(ROOT, config, config["rows"][0])


def _probe_seams(pilot, configured, monkeypatch):
    workspace, config = configured
    calls = []

    @contextmanager
    def lock():
        calls.append("lock")
        yield
        calls.append("unlock")

    def state(*args, token=None):
        calls.append("after" if token else "before")
        return {
            "established": True,
            "head": config["runtime_commit"],
            "serving": config["runtime_commit"],
            "bracket": "b1-" + "a" * 24,
        }

    async def run(runtime, recipe, scratch):
        calls.append("run")
        assert not (workspace / ".codex/receipts/7.json").exists()
        return tostring(_xml(pilot, recipe))

    monkeypatch.setattr(pilot, "deployment_lock", lock)
    monkeypatch.setattr(pilot, "bound_state", state)
    monkeypatch.setattr(pilot, "_run_recipe", run)
    return calls


def test_complete_probe_retires_then_publishes_private_receipt(pilot, configured, monkeypatch):
    workspace, config = configured
    target = workspace / ".codex/receipts/7.json"
    target.write_text("previous complete receipt")
    calls = _probe_seams(pilot, configured, monkeypatch)
    result = pilot.probe(workspace, ROOT, 7)
    receipt = pilot._private_json(target)
    assert calls == ["lock", "before", "run", "after", "unlock"]
    assert receipt["configuration"] == pilot.digest(config)
    assert receipt["row"] == config["rows"][0] and len(receipt["cases"]) == 2
    assert result["receipt"] == pilot.digest(receipt) and result["preview_only"]
    assert target.stat().st_mode & 0o077 == 0
    assert not list((workspace / ".codex").glob("probe-*"))


@pytest.mark.parametrize("phase", ["lock", "before", "run", "after", "write"])
def test_failed_probe_never_publishes_new_success(pilot, configured, monkeypatch, phase):
    workspace, _ = configured
    target = workspace / ".codex/receipts/7.json"
    target.write_text("old receipt")
    _probe_seams(pilot, configured, monkeypatch)

    def refuse(*args, **kwargs):
        raise ValueError("fixture refused")

    if phase == "lock":
        monkeypatch.setattr(pilot, "deployment_lock", refuse)
    elif phase in {"before", "after"}:
        previous = pilot.bound_state

        def state(*args, token=None):
            if (token is not None) == (phase == "after"):
                refuse()
            return previous(*args, token=token)

        monkeypatch.setattr(pilot, "bound_state", state)
    elif phase == "run":

        async def refuse_run(*args):
            refuse()

        monkeypatch.setattr(pilot, "_run_recipe", refuse_run)
    else:
        monkeypatch.setattr(pilot, "_publish_receipt", refuse)
    with pytest.raises(ValueError, match="fixture refused"):
        pilot.probe(workspace, ROOT, 7)
    if phase in {"lock", "before"}:
        assert target.read_text() == "old receipt"
    else:
        assert not target.exists()
    assert not list((workspace / ".codex").glob("probe-*"))


def test_shared_lock_refuses_contention_and_closes(pilot, tmp_path, monkeypatch):
    import fcntl

    lock = tmp_path / ".genesis/locks/update.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    monkeypatch.setattr(pilot.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError), pilot.deployment_lock():
            pytest.fail("exclusive deployment lock admitted probe")
    finally:
        os.close(fd)
    with pilot.deployment_lock():
        pass


@pytest.mark.parametrize("fault", ["missing", "symlink", "directory", "fifo"])
def test_shared_lock_refuses_invalid_existing_artifact(pilot, tmp_path, monkeypatch, fault):
    lock = tmp_path / ".genesis/locks/update.lock"
    lock.parent.mkdir(parents=True)
    monkeypatch.setattr(pilot.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    if fault == "symlink":
        target = tmp_path / "other.lock"
        target.touch()
        lock.symlink_to(target)
    elif fault == "directory":
        lock.mkdir()
    elif fault == "fifo":
        os.mkfifo(lock)
    with pytest.raises((OSError, ValueError)), pilot.deployment_lock():
        pytest.fail("invalid deployment lock admitted")


def test_unknown_row_preserves_receipt_without_probe_entry(pilot, configured, monkeypatch):
    workspace, _ = configured
    target = workspace / ".codex/receipts/7.json"
    target.write_text("old receipt")
    monkeypatch.setattr(pilot, "deployment_lock", lambda: pytest.fail("unknown row entered lock"))
    with pytest.raises(ValueError, match="row unavailable"):
        pilot.probe(workspace, ROOT, 8)
    assert target.read_text() == "old receipt"


def test_parallel_operation_refuses_without_retiring_receipt(pilot, configured, monkeypatch):
    workspace, _ = configured
    target = workspace / ".codex/receipts/7.json"
    target.write_text("old receipt")
    monkeypatch.setattr(
        pilot, "deployment_lock", lambda: pytest.fail("parallel operation entered deployment lock")
    )
    with pilot.operation_lock(workspace), pytest.raises(BlockingIOError):
        pilot.probe(workspace, ROOT, 7)
    assert target.read_text() == "old receipt"
    with pilot.operation_lock(workspace):
        pass


@pytest.mark.parametrize(
    "payload",
    [
        {"version": True, "operation": "pilot_packet"},
        {"version": 1, "operation": "pilot_packet", "pr": 7},
        {"version": 1, "operation": "pilot_probe", "pr": True},
        {"version": 1, "operation": "pilot_probe", "pr": 0},
        {"version": 1, "operation": "pilot_probe", "pr": 7, "argv": ["shell"]},
        {"version": 1, "operation": "pilot_probe", "pr": 7, "db_path": "/outside.db"},
    ],
)
def test_dispatch_rejects_before_pilot_entry(pilot, tmp_path, monkeypatch, payload):
    import sys

    runner = private_module(
        "codex_pilot_runner_under_test", ROOT / "scripts/codex_validator_request.py"
    )
    stub = SimpleNamespace(
        packet=lambda *a: pytest.fail("rejected packet entered"),
        probe=lambda *a: pytest.fail("rejected probe entered"),
    )
    monkeypatch.setitem(sys.modules, "codex_validator_pilot", stub)
    with pytest.raises(ValueError):
        runner.execute(payload, workspace=tmp_path)


def test_dispatch_requires_workspace(pilot):
    runner = private_module(
        "codex_pilot_runner_no_workspace", ROOT / "scripts/codex_validator_request.py"
    )
    with pytest.raises(ValueError, match="Unsupported pilot"):
        runner.execute({"version": 1, "operation": "pilot_packet"})


@pytest.mark.parametrize("ending", ["timeout", "cancel", "repeat_cancel"])
def test_recipe_shutdown_allows_governor_to_stop_separate_child(pilot, tmp_path, monkeypatch, ending):
    import asyncio
    import signal
    import sys
    from contextlib import suppress

    actual_create = asyncio.create_subprocess_exec
    ready = tmp_path / "owned-child.pid"
    launched = []
    body = (
        "import pathlib,signal,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)\n"
        "def stop(*args):\n"
        " time.sleep(.3);child.terminate();child.wait(timeout=5);raise SystemExit(143)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "pending=pathlib.Path(sys.argv[1]).with_suffix('.pending')\n"
        "pending.write_text(str(child.pid));pending.replace(sys.argv[1])\n"
        "time.sleep(30)\n"
    )

    async def fixture_governor(*argv, **kwargs):
        proc = await actual_create(sys.executable, "-I", "-c", body, str(ready), **kwargs)
        launched.append(proc)
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_governor)
    monkeypatch.setattr(pilot, "PROBE_TIMEOUT", 0.1 if ending == "timeout" else 10)

    async def check():
        task = asyncio.create_task(pilot._run_recipe(ROOT, "queue_snapshot", tmp_path))
        if ending != "timeout":
            async with asyncio.timeout(5):
                while not ready.exists():
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            task.cancel()
            if ending == "repeat_cancel":
                await asyncio.sleep(0.02)
                task.cancel()
        with pytest.raises(TimeoutError if ending == "timeout" else asyncio.CancelledError):
            await task
        assert launched[0].returncode == 143
        with pytest.raises(ProcessLookupError):
            os.kill(int(ready.read_text()), 0)

    try:
        asyncio.run(check())
    finally:
        if ready.exists():
            with suppress(ProcessLookupError):
                os.kill(int(ready.read_text()), signal.SIGTERM)


@pytest.mark.parametrize(
    "fault", ["success", "exit", "stdout", "stderr", "timeout", "oversize_xml", "missing_xml"]
)
def test_fixed_recipe_child_environment_bounds_and_cleanup(pilot, tmp_path, monkeypatch, fault):
    import asyncio
    import sys

    actual_create = asyncio.create_subprocess_exec
    launched = []
    captured = {}
    payload = tostring(_xml(pilot))

    async def fixture_child(*argv, **kwargs):
        captured.update(argv=argv, env=kwargs["env"])
        junit = argv[-1].split("=", 1)[1]
        body = "import pathlib,sys,time\n"
        if fault != "missing_xml":
            data = b"x" * (pilot.LIMIT + 1) if fault == "oversize_xml" else payload
            body += f"pathlib.Path(sys.argv[1]).write_bytes({data!r})\n"
        if fault in {"stdout", "stderr"}:
            body += (
                "print('x'*65537, file="
                + ("sys.stdout" if fault == "stdout" else "sys.stderr")
                + ")\n"
            )
        if fault == "timeout":
            body += "time.sleep(30)\n"
        if fault == "exit":
            body += "raise SystemExit(1)\n"
        child = await actual_create(sys.executable, "-I", "-c", body, junit, **kwargs)
        launched.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_child)
    if fault == "timeout":
        monkeypatch.setattr(pilot, "PROBE_TIMEOUT", 0.2)

    async def check():
        if fault == "success":
            assert await pilot._run_recipe(ROOT, "queue_snapshot", tmp_path) == payload
        else:
            with pytest.raises((ValueError, TimeoutError, FileNotFoundError)):
                await pilot._run_recipe(ROOT, "queue_snapshot", tmp_path)
        assert len(launched) == 1 and launched[0].returncode is not None

    asyncio.run(check())
    install_home = Path(pilot.pwd.getpwuid(os.getuid()).pw_dir)
    assert captured["env"]["HOME"] == str(install_home)
    assert "GENESIS_HOME" not in captured["env"]
    argv = list(captured["argv"])
    separator = argv.index("--")
    assert argv[:separator] == [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "genesis.hostmetrics",
        "run",
        "--name",
        "codex-validator-pilot",
        "--ram",
        "1.5",
        "--cpu",
        "100",
        "--wait-until-fits",
        "15",
    ]
    assert argv[separator + 1 : separator + 6] == [
        "/usr/bin/env",
        f"HOME={tmp_path / 'home'}",
        f"GENESIS_HOME={tmp_path / 'home/.genesis'}",
        "GENESIS_PYTEST_LOCK_WAIT=1",
        f"GENESIS_PYTEST_LOCK_PATH={install_home / '.genesis/locks/pytest.lock'}",
    ]
    assert argv[separator + 6 : -2] == [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "pytest",
        *pilot.RECIPES["queue_snapshot"][0],
    ]
