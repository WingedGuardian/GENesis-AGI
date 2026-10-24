"""Replay receipts must account for the entire selected test lifecycle."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_hooks import codex_adapter_replay as bridge
from tests.test_hooks.test_codex_validator_adapter_native import source_copy


def replay_config(root):
    options = {"--codex-replay-receipt": str(root / "receipt.json"),
               "basetemp": str(root / "run")}
    return SimpleNamespace(getoption=lambda name: options[name])


def test_configuration_owns_import_time_home_and_restores_process(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "tmp"
    root.mkdir()
    config = replay_config(root)
    old_home, old_temp, old_tmpdir = os.environ.get("HOME"), tempfile.tempdir, os.environ.get("TMPDIR")
    try:
        bridge.pytest_configure(config)
        sidecar = root / "run.fixtures"
        home = config._codex_replay_state["owned_home"]
        assert os.environ["HOME"] == str(home)
        assert home.parent == Path("/tmp") and len(home.parts) == 3
        assert home.stat().st_mode & 0o777 == 0o700
        assert tempfile.gettempdir() == str(sidecar / "temp")
        assert sidecar.stat().st_mode & 0o777 == 0o700
        assert os.environ.get("TMPDIR") == old_tmpdir
    finally:
        if hasattr(bridge, "pytest_unconfigure"):
            bridge.pytest_unconfigure(config)
    assert os.environ.get("HOME") == old_home and tempfile.tempdir == old_temp
    shutil.rmtree(home)


@pytest.mark.parametrize("occupied", ["run", "run.fixtures", "receipt.json"])
def test_configuration_refuses_reused_run_paths(monkeypatch, tmp_path, occupied):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "tmp"
    root.mkdir()
    (root / occupied).touch()
    config = replay_config(root)
    try:
        with pytest.raises(pytest.UsageError, match="fresh"):
            bridge.pytest_configure(config)
    finally:
        if hasattr(bridge, "pytest_unconfigure"):
            bridge.pytest_unconfigure(config)


def test_configuration_restores_process_after_partial_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "tmp"
    root.mkdir()
    config = replay_config(root)
    old_home, old_temp = os.environ.get("HOME"), tempfile.tempdir
    mkdir = Path.mkdir
    mkdtemp = tempfile.mkdtemp
    allocated = []

    def allocate(*args, **kwargs):
        path = mkdtemp(*args, **kwargs)
        allocated.append(Path(path))
        return path

    def fail_temp(path, *args, **kwargs):
        if path == root / "run.fixtures/temp":
            raise OSError("fixture creation failed")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_temp)
    monkeypatch.setattr(tempfile, "mkdtemp", allocate)
    try:
        with pytest.raises(OSError, match="fixture creation failed"):
            bridge.pytest_configure(config)
        assert os.environ.get("HOME") == old_home and tempfile.tempdir == old_temp
        assert len(allocated) == 1 and not allocated[0].exists()
    finally:
        if hasattr(bridge, "pytest_unconfigure"):
            bridge.pytest_unconfigure(config)
    assert os.environ.get("HOME") == old_home and tempfile.tempdir == old_temp
    assert len(allocated) == 1 and not allocated[0].exists()


def test_configuration_retires_home_rejected_before_registration(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "tmp"
    root.mkdir()
    config = replay_config(root)
    old_home, old_temp = os.environ.get("HOME"), tempfile.tempdir
    mkdtemp = tempfile.mkdtemp
    allocated = []

    def invalid_home(*args, **kwargs):
        path = Path(mkdtemp(*args, **kwargs))
        path.chmod(0o755)
        allocated.append(path)
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", invalid_home)
    try:
        with pytest.raises(pytest.UsageError, match="fresh owned shallow HOME"):
            bridge.pytest_configure(config)
        assert len(allocated) == 1 and not allocated[0].exists(), "Rejected HOME allocation leaked"
        assert os.environ.get("HOME") == old_home and tempfile.tempdir == old_temp
    finally:
        if hasattr(bridge, "pytest_unconfigure"):
            bridge.pytest_unconfigure(config)
        for path in allocated:
            if path.exists():
                shutil.rmtree(path)


@pytest.mark.parametrize("invalid", ["relative", "outside", "tmp-root", "symlink-escape"])
def test_configuration_refuses_unowned_base(monkeypatch, tmp_path, invalid):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "tmp"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    options = {"--codex-replay-receipt": str(root / "receipt.json"),
               "basetemp": {"relative": "relative-run", "outside": str(outside / "run"),
                            "tmp-root": str(root), "symlink-escape": str(root / "escape/run")}[invalid]}
    config = SimpleNamespace(getoption=lambda name: options[name])
    old_home, old_temp = os.environ.get("HOME"), tempfile.tempdir
    try:
        with pytest.raises(pytest.UsageError):
            bridge.pytest_configure(config)
    finally:
        if hasattr(bridge, "pytest_unconfigure"):
            bridge.pytest_unconfigure(config)
    assert os.environ.get("HOME") == old_home and tempfile.tempdir == old_temp
    assert not (outside / "run").exists()


def test_replay_executes_relative_fixture_from_subprocess_cwd(monkeypatch, tmp_path):
    runtime = source_copy(tmp_path / "source")
    fixture = tmp_path / "fixture"
    guard = fixture / "scripts/hooks/git_discard_guard.py"
    guard.parent.mkdir(parents=True)
    guard.write_text('import sys; print("BLOCKED: fixture-owned-path", file=sys.stderr); sys.exit(2)\n')
    sidecar = tmp_path / "sidecar"
    for name in ("home", "temp"):
        (sidecar / name).mkdir(parents=True)
    monkeypatch.setattr(bridge, "ROWS", [])
    request = SimpleNamespace(
        config=SimpleNamespace(_codex_replay_state={"sidecar": sidecar, "ambient_tmpdir": os.environ.get("TMPDIR")}),
        node=SimpleNamespace(originalname="test_relative_fixture", nodeid="fixture::relative"),
        module=SimpleNamespace(__name__="fixture"),
    )
    factory = SimpleNamespace(getbasetemp=lambda: tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        case = bridge.replay_case(request, patch, tmp_path, factory, runtime)
        next(case)
        try:
            result = subprocess.run(
                [sys.executable, "scripts/hooks/git_discard_guard.py"], cwd=fixture,
                input=json.dumps({"tool_name": "Bash", "cwd": str(fixture),
                                  "tool_input": {"command": "echo fixture"}}),
                env={"PATH": os.environ["PATH"], "HOME": str(sidecar / "home")},
                capture_output=True, text=True,
            )
            assert result.returncode == 2 and "fixture-owned-path" in result.stderr
            assert bridge.ROWS[-1]["actual_target"] == str(guard)
        finally:
            with pytest.raises(StopIteration):
                next(case)


@pytest.mark.parametrize("name", ["git_push_guard", "secrets_env_access_guard"])
def test_claude_dispatched_cases_retain_their_direct_assertions(monkeypatch, tmp_path, name):
    runtime = source_copy(tmp_path / "source")
    guard = tmp_path / "fixture" / (name + ".py")
    guard.parent.mkdir()
    guard.write_text(
        "import os,sys\n"
        "if os.environ.get('GENESIS_CC_SESSION') == '1':\n"
        " print('fixture-dispatched-denial',file=sys.stderr);sys.exit(2)\n"
    )
    sidecar = tmp_path / "sidecar"
    sidecar.mkdir()
    (sidecar / "temp").mkdir()
    monkeypatch.setattr(bridge, "ROWS", [])
    request = SimpleNamespace(
        config=SimpleNamespace(_codex_replay_state={"sidecar": sidecar, "ambient_tmpdir": None}),
        node=SimpleNamespace(originalname="test_dispatched", nodeid="fixture::dispatched"),
        module=SimpleNamespace(__name__="fixture"),
    )
    factory = SimpleNamespace(getbasetemp=lambda: tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        case = bridge.replay_case(request, patch, tmp_path, factory, runtime)
        next(case)
        try:
            result = subprocess.run(
                [sys.executable, str(guard)], cwd=tmp_path,
                input=json.dumps({"tool_name": "Bash", "cwd": str(tmp_path),
                                  "tool_input": {"command": "echo fixture"}}),
                env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GENESIS_CC_SESSION": "1"},
                capture_output=True, text=True, timeout=8,
            )
            assert result.returncode == 2 and "fixture-dispatched-denial" in result.stderr
            assert bridge.ROWS[-1]["kind"] != "replay"
            assert "Claude dispatched-session" in bridge.ROWS[-1]["reason"]
        finally:
            with pytest.raises(StopIteration):
                next(case)


@pytest.mark.parametrize("overlap", ["equal-base", "equal-sidecar", "inside-base", "inside-sidecar"])
def test_configuration_refuses_overlapping_receipt(monkeypatch, tmp_path, overlap):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "tmp"
    root.mkdir()
    values = {"equal-base": root / "run", "equal-sidecar": root / "run.fixtures",
              "inside-base": root / "run/receipt.json", "inside-sidecar": root / "run.fixtures/receipt.json"}
    options = {"basetemp": str(root / "run"), "--codex-replay-receipt": str(values[overlap])}
    config = SimpleNamespace(getoption=lambda name: options[name])
    try:
        with pytest.raises(pytest.UsageError, match="separate"):
            bridge.pytest_configure(config)
    finally:
        if hasattr(bridge, "pytest_unconfigure"):
            bridge.pytest_unconfigure(config)


@pytest.mark.parametrize("fault", [None, "empty", "deny", "reason"])
def test_remote_force_oracle_precedes_mutation_and_checks_reason(monkeypatch, tmp_path, fault):
    runtime = source_copy(tmp_path / "source")
    guard = tmp_path / "fixture/git_push_guard.py"
    guard.parent.mkdir()
    guard.write_text(
        "import json,sys\n"
        "data=json.load(sys.stdin)\n"
        f"if {fault == 'reason'}:\n"
        " reason='replacement' if data['tool_input']['command'].startswith('cd ') else 'fixture nonforce approval'\n"
        " print(json.dumps({'hookSpecificOutput':{'permissionDecision':'ask','permissionDecisionReason':reason}}))\n"
        " sys.exit(0)\n"
        "if data['tool_input']['command'].startswith('cd '):\n"
        " print('BLOCKED: a force push off the public repo must be the whole command',file=sys.stderr)\n"
        " sys.exit(2)\n"
        "print(json.dumps({'hookSpecificOutput':{'permissionDecision':'ask',"
        "'permissionDecisionReason':'FORCE push detected fixture'}}))\n"
    )
    sidecar = tmp_path / "sidecar"
    (sidecar / "temp").mkdir(parents=True)
    monkeypatch.setattr(bridge, "ROWS", [])
    monkeypatch.delenv("ADAPTER_REPLAY_CHILD_FAULT", raising=False)
    if fault in {"empty", "deny"}:
        monkeypatch.setenv("ADAPTER_REPLAY_CHILD_FAULT", fault)
    request = SimpleNamespace(
        config=SimpleNamespace(_codex_replay_state={"sidecar": sidecar, "ambient_tmpdir": None}),
        node=SimpleNamespace(originalname="test_remote_probe", nodeid="fixture::remote"),
        module=SimpleNamespace(__name__="tests.test_hooks.test_worktree_remote_guard"),
    )
    factory = SimpleNamespace(getbasetemp=lambda: tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        case = bridge.replay_case(request, patch, tmp_path, factory, runtime)
        next(case)
        try:
            def invoke():
                return subprocess.run(
                    [sys.executable, str(guard)], cwd=tmp_path,
                    input=json.dumps({"tool_name": "Bash", "cwd": str(tmp_path),
                                      "tool_input": {"command": "git push backups main" if fault == "reason"
                                                     else "git push --force backups main"}}),
                    env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                    capture_output=True, text=True, timeout=8,
                )
            if fault:
                marker = {"empty": "changed admission", "deny": "force reason lost",
                          "reason": "approval reason lost"}[fault]
                with pytest.raises(AssertionError, match=marker):
                    invoke()
            else:
                result = invoke()
                assert result.returncode == 0
                assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
                assert not bridge.ROWS[-1]["actual_allow"]
            assert bridge.ROWS[-1]["baseline_decision"] == "ask"
            assert bridge.ROWS[-1]["expected_force"] == (fault != "reason")
        finally:
            with pytest.raises(StopIteration):
                next(case)


def test_owned_runtime_preserves_executable_entrypoint(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "COPIED", {})
    config = SimpleNamespace(_codex_replay_state={})
    runtime = bridge.existing_replay_runtime.__wrapped__(
        SimpleNamespace(mktemp=lambda _: tmp_path), SimpleNamespace(config=config)
    )
    launcher = runtime / ".claude/hooks/genesis-hook"
    result = subprocess.run(
        [str(launcher), "hooks/git_discard_guard.py"], cwd=tmp_path,
        input=json.dumps({"tool_name": "Bash", "cwd": str(tmp_path),
                          "tool_input": {"command": "echo fixture"}}),
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "GENESIS_HOME": str(tmp_path / ".genesis"), "GENESIS_DB_PATH": str(tmp_path / "absent.db")},
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stderr


def test_fixture_manifest_binds_public_import_in_sibling_without_healing(tmp_path):
    guard = tmp_path / "hooks/git_discard_guard.py"
    guard.parent.mkdir()
    guard.write_text("import shell_parse\n")
    reference = bridge.ROOT / "scripts/hooks/shell_parse.py"
    sibling = guard.parent / "shell_parse.py"
    sibling.write_text(f"PUBLIC_SOURCE = {str(reference)!r}\n")
    missing = guard.parent / "discarded_write.py"
    manifest = bridge.fixture_manifest(guard)
    assert manifest["public_source_references"] == {"scripts/hooks/shell_parse.py": bridge.sha(reference)}
    assert "discarded_write.py" in manifest["missing_fixture_sources"]
    assert not missing.exists()
    assert manifest["fixture_source_hashes"]["shell_parse.py"] == bridge.sha(sibling)


@pytest.mark.parametrize("fault", [None, "empty", "deny", "digest-only", "mixed-empty", "mixed-digest"])
@pytest.mark.parametrize(
    "phases,expected",
    [
        ([("setup", "passed"), ("call", "passed"), ("teardown", "passed")], "passed"),
        ([("setup", "skipped"), ("teardown", "passed")], "skipped"),
        ([("setup", "failed"), ("teardown", "passed")], "failed"),
        ([("setup", "passed"), ("call", "failed"), ("teardown", "passed")], "failed"),
        ([("setup", "passed"), ("call", "passed"), ("teardown", "failed")], "failed"),
        ([("setup", "passed"), ("call", "skipped"), ("teardown", "passed")], "skipped"),
        ([], "not-run"),
    ],
)
def test_receipt_keeps_every_selected_node_and_qualifies_only_complete_passes(
    monkeypatch, tmp_path, phases, expected, fault
):
    nodeid = "tests/fixture.py::test_case"
    monkeypatch.setattr(bridge, "REPORTS", {})
    monkeypatch.setattr(bridge, "SOURCE_ITEMS", {})
    monkeypatch.setattr(bridge, "COPIED", {})
    real = {"nodeid": nodeid, "kind": "replay", "adapter_assertions_passed": True}
    control = real.copy()
    if fault in {"digest-only", "mixed-digest"}:
        control["injected_guard_sha256"] = "0" * 64
    elif fault:
        control["child_fault"] = "empty" if fault == "mixed-empty" else fault
    mixed = fault is not None and fault.startswith("mixed-")
    rows = [real] if fault is None else [real, control] if mixed else [control]
    monkeypatch.setattr(bridge, "ROWS", rows)
    path = tmp_path / "receipt.json"
    item = SimpleNamespace(
        nodeid=nodeid, location=("tests/fixture.py", 7, "test_case"), function=lambda: None
    )
    session = SimpleNamespace(items=[item], config=SimpleNamespace(getoption=lambda _: str(path)))
    # Older instrument versions had no collection seed: keep the regression
    # observable as a missing receipt node, not an AttributeError in the test.
    if hasattr(bridge, "pytest_collection_finish"):
        bridge.pytest_collection_finish(session)
    for when, outcome in phases:
        report = SimpleNamespace(
            when=when,
            outcome=outcome,
            skipped=outcome == "skipped",
            longrepr=("tests/fixture.py", 8, "Skipped: fixture") if outcome == "skipped" else None,
        )
        hook = bridge.pytest_runtest_makereport(item, None)
        next(hook)
        with pytest.raises(StopIteration):
            hook.send(SimpleNamespace(get_result=lambda report=report: report))
    bridge.pytest_sessionfinish(session, 0)
    receipt = json.loads(path.read_text())
    assert set(receipt["coverage"]) == {nodeid}
    row = receipt["coverage"][nodeid]
    assert row["direct_test_result"] == expected
    real_replays = int(fault is None or mixed)
    assert row["qualified_replays"] == (real_replays if expected == "passed" else 0)
    assert row["completed_replays"] == real_replays
    assert row["status"] == ("FULL" if fault is None else "PARTIAL" if mixed else "NO")
    if fault:
        assert any("instrument control" in reason for reason in row["exclusions"])


def test_receipt_invalidates_instrument_changed_after_load(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "REPORTS", {})
    monkeypatch.setattr(bridge, "SOURCE_ITEMS", {})
    monkeypatch.setattr(bridge, "COPIED", {})
    monkeypatch.setattr(bridge, "ROWS", [])
    monkeypatch.setattr(bridge, "sha", lambda _: "changed-after-import")
    path = tmp_path / "receipt.json"
    session = SimpleNamespace(
        items=[], config=SimpleNamespace(getoption=lambda _: str(path)), exitstatus=0
    )
    bridge.pytest_sessionfinish(session, 0)
    assert session.exitstatus != 0, "A changed instrument was still accepted as the executed source"
    assert json.loads(path.read_text()).get("instrument_unchanged", True) is False
