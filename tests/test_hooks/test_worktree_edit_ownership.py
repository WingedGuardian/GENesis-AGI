"""Owned native sessions/worktrees exercise the shipped nonblocking edit hook."""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts/hooks/worktree_ownership_advisory.py"
spec = importlib.util.spec_from_file_location("owned_claim", ROOT / "scripts/hooks/worktree_claim.py")
claim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(claim)


@pytest.fixture
def world(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_", "CLAUDE_"))}
    env.update(HOME=str(tmp_path), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    repo = tmp_path / "main"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True).stdout.strip()

    git("-c", "init.templateDir=", "init", "-q")
    tree = git("hash-object", "-t", "tree", os.devnull)
    commit = git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit-tree", tree, "-m", "owned edit fixture")
    worktree = tmp_path / "linked équipe"
    git("worktree", "add", "--detach", str(worktree), commit)
    for relative in (
        "scripts/hooks/worktree_ownership_advisory.py", "scripts/hooks/worktree_claim.py",
        "scripts/hooks/hook_input.py", "scripts/hooks/hook_output.py",
        "src/genesis/util/atomic.py", "config/worktree_ownership.yaml",
        ".claude/hooks/genesis-hook",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    interpreter = repo / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    return repo, worktree, env


def event(worktree, name="PostToolUse", tool="Write", **extra):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"hook_event_name": name, "tool_name": tool, "cwd": str(worktree),
            "tool_input": {key: "new space équipe.txt"}, "tool_response": {}, **extra}


def session(world, events, *, launcher=False, argv0="claude"):
    repo, worktree, env = world
    # This is a small owned process with the exact argv[0] the production
    # ancestry reader recognizes, not a Claude invocation or registered session.
    code = """import json,os,subprocess,sys
results=[]
for event in json.loads(sys.argv[3]):
 p=subprocess.run(json.loads(sys.argv[1]),input=json.dumps(event),text=True,capture_output=True)
 results.append({'code':p.returncode,'out':p.stdout,'err':p.stderr})
print(json.dumps({'pid':os.getpid(),'results':results}),flush=True)
sys.stdin.readline()
"""
    command = [sys.executable, str(repo / "scripts/hooks/worktree_ownership_advisory.py")]
    if launcher:
        env = {**env, "GENESIS_HOOK_DEV_LOCAL": "1"}
        command = [str(repo / ".claude/hooks/genesis-hook"), "hooks/worktree_ownership_advisory.py"]
    process = subprocess.Popen([argv0, "-c", code, json.dumps(command), "unused", json.dumps(events)],
                               executable=sys.executable, cwd=worktree, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        receipt = json.loads(process.stdout.readline())
        yield process, receipt
    finally:
        process.communicate("\n", timeout=10)
        assert process.returncode == 0


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_successful_edit_claim_is_readable_and_own_advice_is_quiet(world, tool):
    _, worktree, _ = world
    runner = session(world, [event(worktree, tool=tool), event(worktree, "PreToolUse", tool)])
    try:
        process, receipt = next(runner)
        assert all(r["code"] == 0 for r in receipt["results"]), receipt
        lock = claim.read_lock(worktree)
        assert lock and not lock.foreign
        assert lock.payload["pid"] == process.pid
        assert lock.payload["start"] == claim.proc_starttime(process.pid)
        assert "sid" not in lock.payload
        assert receipt["results"][1]["out"] == ""
    finally:
        runner.close()


def test_foreign_live_claim_advises_without_overwriting(world):
    _, worktree, _ = world
    first = session(world, [event(worktree)])
    try:
        owner, _ = next(first)
        original = claim.read_lock(worktree).raw
        second = session(world, [event(worktree, "PreToolUse"), event(worktree)])
        try:
            _, receipt = next(second)
            assert all(r["code"] == 0 for r in receipt["results"])
            output = json.loads(receipt["results"][0]["out"])["hookSpecificOutput"]
            assert output["hookEventName"] == "PreToolUse"
            assert str(owner.pid) in output["additionalContext"]
            assert "permissionDecision" not in output
            assert claim.read_lock(worktree).raw == original
        finally:
            second.close()
    finally:
        first.close()


@pytest.mark.parametrize("target_kind", ["existing", "new-file", "deep-uncreated"])
@pytest.mark.parametrize("preexisting_owner", [False, True])
def test_nested_standalone_repository_neither_claims_nor_advises_outer(world, target_kind, preexisting_owner):
    _, worktree, env = world
    nested = worktree / "vendor/independent"
    nested.mkdir(parents=True)
    subprocess.run(["git", "-C", str(nested), "-c", "init.templateDir=", "init", "-q"], env=env, check=True)
    target = nested / ("missing/deep/file.txt" if target_kind == "deep-uncreated" else "file.txt")
    if target_kind == "existing":
        target.write_text("owned fixture")
    owner = session(world, [event(worktree)]) if preexisting_owner else None
    try:
        if owner is not None:
            next(owner)
        original = claim.read_lock(worktree)
        raw = original.raw if original else None
        probe = session(world, [
            event(worktree, "PreToolUse", tool_input={"file_path": str(target)}),
            event(worktree, tool_input={"file_path": str(target)}),
        ])
        try:
            _, receipt = next(probe)
            assert all(row["code"] == 0 and row["out"] == "" for row in receipt["results"]), receipt
            current = claim.read_lock(worktree)
            assert (current.raw if current else None) == raw
            assert not (nested / ".git/locked").exists()
        finally:
            probe.close()
    finally:
        if owner is not None:
            owner.close()


@pytest.mark.parametrize("reason", ["foreign owner", "reaper archived worktree"])
def test_existing_nonclaim_lock_stays_untouched(world, reason):
    repo, worktree, env = world
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", "--reason", reason, str(worktree)], env=env, check=True)
    runner = session(world, [event(worktree, "PreToolUse"), event(worktree)])
    try:
        _, receipt = next(runner)
        assert all(r["code"] == 0 and r["out"] == "" for r in receipt["results"])
        assert claim.read_lock(worktree).raw == reason
    finally:
        runner.close()


def test_atomic_claim_preserves_one_concurrent_winner(world, monkeypatch):
    _, worktree, _ = world
    barrier = threading.Barrier(2)
    read = claim.read_lock

    def simultaneous_read(root):
        found = read(root)
        barrier.wait()
        return found

    real_git = claim._git

    def observed_racy_git(root, *args):
        if args[:2] == ("worktree", "lock"):
            # Deterministically reproduce the native Git2.43 double-success
            # observed in an independent two-process fixture. The new primitive
            # must itself publish exclusively rather than trusting this writer.
            (claim.gitdir_for(root) / "locked").write_text(args[3])
            return subprocess.CompletedProcess(args, 0)
        return real_git(root, *args)

    monkeypatch.setattr(claim, "read_lock", simultaneous_read)
    monkeypatch.setattr(claim, "_git", observed_racy_git)
    payloads = [claim.build_payload(pid=os.getpid(), sid=sid) for sid in ("a", "b")]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda payload: claim.lock_worktree(worktree, payload), payloads))
    assert sorted(results) == [False, True]
    winner = read(worktree)
    assert winner.payload == payloads[results.index(True)]


def test_settings_wire_both_events_and_all_supported_tools():
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    for name in ("PreToolUse", "PostToolUse"):
        matches = [row for row in settings["hooks"][name] if any("worktree_ownership_advisory.py" in h.get("command", "") for h in row["hooks"])]
        assert len(matches) == 1
        assert matches[0]["matcher"] == "^(Write|Edit|MultiEdit|NotebookEdit)$"


@pytest.mark.parametrize("change", [
    {"hook_event_name": "PostToolUseFailure"}, {"tool_name": "Bash"},
    {"tool_input": {"file_path": ""}}, {"tool_input": {"file_path": None}},
    {"tool_response": {"is_error": True}}, {"tool_response": {"error": "fixture"}},
    {"cwd": "relative"}, {"tool_response": "unavailable"},
])
def test_unsuccessful_or_unsupported_events_do_not_claim(world, change):
    _, worktree, _ = world
    runner = session(world, [event(worktree, **change)])
    try:
        _, receipt = next(runner)
        assert receipt["results"] == [{"code": 0, "out": "", "err": ""}]
        assert claim.read_lock(worktree) is None
    finally:
        runner.close()


@pytest.mark.parametrize("switch", ["env", "settings"])
def test_disable_levers_prevent_claims(world, switch):
    _, worktree, env = world
    if switch == "env":
        env["GENESIS_WORKTREE_OWNERSHIP"] = "1"
    else:
        config = Path(env["HOME"]) / ".genesis/config"
        config.mkdir(parents=True)
        (config / "worktree_ownership.local.yaml").write_text("enabled: false\n")
    runner = session(world, [event(worktree)])
    try:
        _, receipt = next(runner)
        assert receipt["results"] == [{"code": 0, "out": "", "err": ""}]
        assert claim.read_lock(worktree) is None
    finally:
        runner.close()


@pytest.mark.parametrize("observed", [None, 98, 100])
def test_unknown_dead_or_reused_owner_is_not_accused(world, monkeypatch, capsys, observed):
    _, worktree, _ = world
    monkeypatch.syspath_prepend(str(ROOT / "scripts/hooks"))
    import worktree_ownership_advisory as hook

    monkeypatch.setattr(hook, "__file__", str(world[0] / "scripts/hooks/worktree_ownership_advisory.py"))
    monkeypatch.setitem(sys.modules, "worktree_claim", claim)
    monkeypatch.setattr("hook_input.read_payload", lambda: event(worktree, "PreToolUse"))
    monkeypatch.setattr(claim, "effective_mode", lambda: "advisory")
    monkeypatch.setattr(claim, "session_pid_from_ancestry", lambda: 123)
    monkeypatch.setattr(claim, "read_lock", lambda root: claim.Lock("", {"pid": 456, "start": 99}, False))
    monkeypatch.setattr(claim, "proc_starttime", lambda pid: observed)
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_unattributable_external_process_never_mints_a_claim(world):
    _, worktree, env = world
    code = """import os,sys,json,subprocess,time
parent=os.getpid()
if os.fork(): os._exit(0)
while os.getppid()==parent: time.sleep(.001)
worker="import os,sys,json,subprocess; result=subprocess.run([sys.argv[1],sys.argv[2]],input=sys.argv[3],text=True,capture_output=True); print(json.dumps({'original_parent':int(sys.argv[4]),'current_parent':os.getppid(),'code':result.returncode}),flush=True)"
os.execv(sys.argv[1],['orphan-fixture','-c',worker,*sys.argv[1:],str(parent)])
"""
    process = subprocess.Popen(["claude", "-c", code, sys.executable, str(world[0] / "scripts/hooks/worktree_ownership_advisory.py"), json.dumps(event(worktree))],
                               executable=sys.executable, cwd=worktree, env=env, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    receipt = json.loads(out)
    assert receipt["original_parent"] != receipt["current_parent"]
    assert receipt["code"] == 0, err
    assert claim.read_lock(worktree) is None


def test_actual_wrapper_uses_the_requested_branch_hook(world):
    _, worktree, _ = world
    runner = session(world, [event(worktree)], launcher=True)
    try:
        process, receipt = next(runner)
        assert receipt["results"][0]["code"] == 0, receipt
        assert "WORKTREE-local hook copy" in receipt["results"][0]["err"]
        assert claim.read_lock(worktree).payload["pid"] == process.pid
    finally:
        runner.close()


def test_exited_unreaped_owner_is_not_reported_as_live(world):
    _, worktree, env = world
    owner = subprocess.Popen(['claude', '-c', 'import time; time.sleep(.2)'], executable=sys.executable, env=env)
    try:
        payload = claim.build_payload(pid=owner.pid)
        assert payload is not None
        assert claim.lock_worktree(worktree, payload)
        os.waitid(os.P_PID, owner.pid, os.WEXITED | os.WNOWAIT)
        assert claim.proc_starttime(owner.pid) == payload['start']
        runner = session(world, [event(worktree, 'PreToolUse')])
        try:
            _, receipt = next(runner)
            assert receipt['results'][0]['out'] == ''
            assert claim.read_lock(worktree).payload == payload
        finally:
            runner.close()
    finally:
        owner.wait(timeout=5)


@pytest.mark.parametrize('name,edit_peer', [(b'peer-\xff', False), (b'target-\xff', True), (b'target-\r', True), (b'target-\r\n', True)])
def test_registered_worktree_paths_are_lossless(world, name, edit_peer):
    repo, worktree, env = world
    peer = worktree.parent / os.fsdecode(name)
    subprocess.run(['git', '-C', str(worktree), 'worktree', 'add', '--detach', str(peer), 'HEAD'], env=env, capture_output=True, check=True)
    target = peer if edit_peer else worktree
    runner = session((repo, target, env), [event(target)])
    try:
        process, receipt = next(runner)
        assert receipt['results'][0]['code'] == 0
        lock = claim.read_lock(target)
        assert lock is not None
        assert lock.payload['pid'] == process.pid
        native_dir = subprocess.check_output(
            ['git', '-C', str(target), 'rev-parse', '--absolute-git-dir'], env=env,
        ).removesuffix(b'\n')
        assert claim.gitdir_for(target) == Path(os.fsdecode(native_dir))
        assert (Path(os.fsdecode(native_dir)) / 'locked').is_file()
        refused = subprocess.run(['git', '-C', str(repo), 'worktree', 'remove', str(target)], env=env, capture_output=True)
        assert refused.returncode != 0 and b'locked' in refused.stderr
        assert claim.unlock_worktree(target, expect_pid=process.pid)
        assert not (Path(os.fsdecode(native_dir)) / 'locked').exists()
    finally:
        runner.close()


@pytest.mark.parametrize("pointer_kind", ["copied", "symlink"])
def test_inconsistent_pointer_cannot_claim_another_registration(world, pointer_kind):
    repo, worktree, env = world
    peer = worktree.parent / 'other-registration'
    subprocess.run(['git', '-C', str(worktree), 'worktree', 'add', '--detach', str(peer), 'HEAD'], env=env, capture_output=True, check=True)
    native_dirs = [Path(os.fsdecode(subprocess.check_output(['git', '-C', str(p), 'rev-parse', '--absolute-git-dir'], env=env).removesuffix(b'\n'))) for p in (worktree, peer)]
    pointer = worktree / '.git'
    original = pointer.read_bytes()
    try:
        if pointer_kind == 'symlink':
            pointer.unlink()
            pointer.symlink_to(peer / '.git')
        else:
            pointer.write_bytes((peer / '.git').read_bytes())
        assert not claim.lock_worktree(worktree, claim.build_payload(pid=os.getpid()))
        assert all(not (p / 'locked').exists() for p in native_dirs)
    finally:
        if pointer.is_symlink():
            pointer.unlink()
        pointer.write_bytes(original)


@pytest.mark.parametrize("override", [None, "ours", "foreign"])
@pytest.mark.parametrize("foreign_cwd", [False, True])
def test_foreign_repository_is_neither_claimed_nor_advised(world, tmp_path, override, foreign_cwd):
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign = globals()["world"].__wrapped__(foreign_root)
    _, target, _ = foreign
    if override:
        selected = world[0] if override == "ours" else foreign[0]
        world[2].update(GIT_DIR=str(selected / ".git"), GIT_COMMON_DIR=str(selected / ".git"), GIT_WORK_TREE=str(selected))
    other_event = event(target if foreign_cwd else world[1], tool_input={"file_path": str(target / "outside.txt")})
    writer = session(world, [other_event])
    try:
        _, receipt = next(writer)
        assert receipt["results"][0]["code"] == 0
        assert claim.read_lock(target) is None
    finally:
        writer.close()
    owner = session(foreign, [event(target)])
    try:
        next(owner)
        original = claim.read_lock(target).raw
        observer = session(world, [{**other_event, "hook_event_name": "PreToolUse"}])
        try:
            _, receipt = next(observer)
            assert receipt["results"][0]["out"] == ""
            assert claim.read_lock(target).raw == original
        finally:
            observer.close()
    finally:
        owner.close()
    # Positive control: inherited location variables cannot disable our own claim.
    test_successful_edit_claim_is_readable_and_own_advice_is_quiet(world, "Edit")


@pytest.mark.parametrize("cwd", [None, "relative", "/definitely/not/a/repository"])
def test_absolute_target_without_resolvable_context_is_not_claimed(world, cwd):
    payload = event(world[1], cwd=cwd, tool_input={"file_path": str(world[1] / "new.txt")})
    runner = session(world, [payload])
    try:
        _, receipt = next(runner)
        assert receipt["results"][0]["code"] == 0
        assert claim.read_lock(world[1]) is None
    finally:
        runner.close()


@pytest.mark.parametrize("argv0", ["claude", "claude.exe", "claude-code", "claude-wrapper"])
def test_native_launcher_closed_set(world, argv0):
    runner = session(world, [event(world[1])], argv0=argv0)
    try:
        process, receipt = next(runner)
        assert receipt["results"][0]["code"] == 0
        lock = claim.read_lock(world[1])
        if argv0 == "claude-wrapper":
            assert lock is None
        else:
            assert lock.payload["pid"] == process.pid
    finally:
        runner.close()


@pytest.mark.parametrize("sid", ["wt-abc123", "session_with_underscores", "a" * 255, "has/slash", "a" * 256, "unsafe\n"])
def test_canonical_sid_retained_and_safe_advice(world, sid):
    safe = sid in {"wt-abc123", "session_with_underscores", "a" * 255}
    assert claim.build_payload(pid=os.getpid(), sid=sid).get("sid") == (sid if safe else None)
    owner = session(world, [event(world[1], session_id=sid)])
    try:
        process, _ = next(owner)
        observer = session(world, [event(world[1], "PreToolUse")])
        try:
            _, receipt = next(observer)
            output = receipt["results"][0]["out"]
            context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
            assert str(process.pid) in context
            assert (sid in context) == safe
            assert len(output.encode()) < 6000
        finally:
            observer.close()
    finally:
        owner.close()


def test_shared_git_runner_preserves_protected_config_and_scrubs_location(world, monkeypatch):
    repo, worktree, _ = world
    monkeypatch.setenv("GIT_DIR", "/not/a/repo")
    monkeypatch.setenv("GIT_WORK_TREE", "/not/a/tree")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "fixture.protected")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "retained")
    result = claim._git(repo, "config", "--get", "fixture.protected")
    assert result.returncode == 0 and result.stdout == "retained\n"
    payload = claim.build_payload(pid=os.getpid())
    assert claim.lock_worktree(worktree, payload)
    assert claim.unlock_worktree(worktree, expect_pid=os.getpid())
