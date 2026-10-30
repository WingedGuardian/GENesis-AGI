"""Opt-in existing-case replay: load explicitly with pytest -p, retain a receipt.

Ordinary cases use one real child for both the original assertions and adapter
decision checks. Named, stateless premise mappings retain a direct guard
control because the mandatory known-cwd anchor changes their old preconditions.
Commands are guard input only; the instrument never executes the action.
"""

import asyncio
import hashlib
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from genesis.session_awareness.zero_drop_git import scrubbed_git_env
from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]
NAMES = {
    "destructive_command_guard",
    "repo_routing_guard",
    "full_suite_guard",
    "tmux_kill_server_guard",
    "pipe_status_guard",
    "credential_surface_hook",
    "git_discard_guard",
    "git_push_guard",
    "secrets_env_access_guard",
    "protected_paths_guard",
    "worktree_cwd_guard",
    "capped_read_advisory",
    "rm_trash_advisory",
    "pr_close_advisory",
    "main_checkout_guard",
}
ROWS = []
REPORTS = {}
COPIED = {}
SOURCE_ITEMS = {}
INSTRUMENT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAPPINGS = {
    "test_relative_without_cwd_falls_back_to_substring": "known-cwd",
    "test_prose_naming_a_supplied_removal_gets_an_honest_refusal": "compound-heredoc",
    "test_a_SINGLE_step_carrier_refusal_does_not_print_it": "compound-note",
    "test_query_file_with_a_merge_is_refused_and_a_plain_one_is_not": "graphql-file",
    "test_input_file_body_is_read": "graphql-file",
}
DIRECT_PUBLISHING_ALLOWANCES = {
    "test_republish_bare_push_allows", "test_republish_explicit_push_allows",
    "test_safe_flags_still_allow", "test_republish_hit_records_branch",
    "test_republish_offline_allow_when_ls_remote_fails",
    "test_integration_record_stays_under_isolated_home",
}
PUBLISHING_TEST_SHA256 = (
    "fd3475fd"
    "2d0d6ab0"
    "3bcba305"
    "53aafcbb"
    "ad673caf"
    "881d0e44"
    "4a6b58df"
    "0afa9ef6"
)
PUBLISHING_GUARD_SHA256 = (
    "a1c42887"
    "bc0fe843"
    "0115d73e"
    "4f86633d"
    "c15b7d3d"
    "60e9df04"
    "33ec986b"
    "89024239"
)


def direct_publishing_allowance(request, guard):
    test_source = ROOT / "tests/test_hooks/test_push_create_override.py"
    if (request.module.__name__ != "tests.test_hooks.test_push_create_override"
            or request.node.originalname not in DIRECT_PUBLISHING_ALLOWANCES
            or Path(getattr(request.module, "__file__", "")).resolve() != test_source
            or guard.resolve() != ROOT / "scripts/hooks/git_push_guard.py"):
        return False
    assert (sha(test_source) == PUBLISHING_TEST_SHA256
            and sha(guard) == PUBLISHING_GUARD_SHA256), "publishing allowance source changed; requalify"
    return True


def pytest_addoption(parser):
    parser.addoption("--codex-replay-receipt", help="Absolute path to the private replay receipt")


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    original_home = Path.home().resolve()
    tmp = (original_home / "tmp").resolve()
    paths = [config.getoption("basetemp"), config.getoption("--codex-replay-receipt")]
    if any(not value or not Path(value).is_absolute() for value in paths):
        raise pytest.UsageError("Explicit replay requires absolute basetemp and receipt")
    base, receipt = map(Path, paths)
    sidecar = base.with_name(base.name + ".fixtures")
    for path in (base, receipt, sidecar):
        resolved = path.resolve()
        if (resolved == tmp or not resolved.is_relative_to(tmp)
                or resolved.is_relative_to(ROOT) or path.exists() or path.is_symlink()):
            raise pytest.UsageError("Replay paths must be fresh owned paths under original HOME/tmp")
    resolved_paths = [path.resolve() for path in (base, receipt, sidecar)]
    if any(a.is_relative_to(b) or b.is_relative_to(a)
           for index, a in enumerate(resolved_paths) for b in resolved_paths[index + 1:]):
        raise pytest.UsageError("Replay paths must be separate fresh paths")
    state = {"home": os.environ.get("HOME"), "tempdir": tempfile.tempdir,
             "ambient_tmpdir": os.environ.get("TMPDIR"),
             "base": base.resolve(), "sidecar": sidecar.resolve()}
    config._codex_replay_state = state
    allocated_home = None
    try:
        sidecar.mkdir(mode=0o700)
        home = Path(tempfile.mkdtemp(prefix="codex-replay-home-", dir="/tmp"))
        allocated_home = home
        if (home.is_symlink() or home.resolve() != home or len(home.parts) != 3
                or home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o777 != 0o700):
            raise pytest.UsageError("Replay needs a fresh owned shallow HOME")
        state["owned_home"] = home
        os.environ["HOME"] = str(home)
        (sidecar / "temp").mkdir()
        tempfile.tempdir = str(sidecar / "temp")
    except BaseException:
        pytest_unconfigure(config)
        if allocated_home is not None:
            shutil.rmtree(allocated_home)
        raise


def pytest_unconfigure(config):
    state = getattr(config, "_codex_replay_state", None)
    if state is not None:
        if state["home"] is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = state["home"]
        tempfile.tempdir = state["tempdir"]
        del config._codex_replay_state


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # Model separate guard invocations; explicit fixtures/body deadlines win.
    for name in ("_mod", "gpg"):
        module = getattr(item.module, name, None)
        if (getattr(module, "__file__", "").endswith("/git_push_guard.py")
                and hasattr(module, "_merge_deadline")):
            module._merge_deadline = None


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_manifest(guard):
    """Describe the actual fixture tree; never copy missing/poisoned helpers in."""
    hooks = guard.parent
    parent_layout = hooks.name == "hooks" and hooks.parent.name in {"scripts", "genesis-scripts"}
    root = hooks.parent if parent_layout else hooks
    canonical = ROOT / "scripts" if parent_layout else ROOT / "scripts/hooks"
    actual = {str(p.relative_to(root)): sha(p) for p in root.rglob("*")
              if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
              and not any(part.is_symlink() for part in p.parents if part != root)}
    expected = {str(p.relative_to(canonical)): sha(p) for p in canonical.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and not p.is_symlink()}
    texts = [str((root / name).read_text()) for name in actual if name.endswith(".py")]
    references = {str(p.relative_to(ROOT)): sha(p) for p in (ROOT / "scripts/hooks").glob("*.py")
                  if any(str(p) in text for text in texts)}
    return {"fixture_root": str(root), "fixture_source_hashes": actual,
            "missing_fixture_sources": sorted(set(expected) - set(actual)),
            "changed_fixture_sources": sorted(k for k in expected.keys() & actual.keys()
                                               if expected[k] != actual[k]),
            "public_source_references": references}


@pytest.fixture(scope="session")
def existing_replay_runtime(tmp_path_factory, request):
    runtime = tmp_path_factory.mktemp("existing-replay-source") / "runtime"
    runtime.mkdir()
    (runtime / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    tracked = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "scripts", ".claude/hooks"], env=scrubbed_git_env()
    ).decode().split("\0")
    for relative in filter(None, tracked):
        source = ROOT / relative
        assert source.is_file() and not source.is_symlink()
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # The original credential fixtures use the real admission predicate and
    # scratch SQLite. Preserve those imports; never replace the fence by a stub.
    for relative in (
        "src/genesis/__init__.py",
        "src/genesis/eval/__init__.py",
        "src/genesis/eval/qualification/__init__.py",
        "src/genesis/eval/qualification/evidence.py",
        "src/genesis/env.py",
        "src/genesis/db/__init__.py",
        "src/genesis/db/admission.py",
        "src/genesis/db/integrity.py",
        "config/repo_topology.yaml",
    ):
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    for part in ("scripts", ".claude/hooks", "src/genesis", "config"):
        for file in (runtime / part).rglob("*"):
            if file.is_file() and "__pycache__" not in file.parts:
                relative = str(file.relative_to(runtime))
                COPIED[relative] = sha(file)
                assert COPIED[relative] == sha(ROOT / relative)
    request.config._codex_replay_runtime = runtime
    request.config._codex_replay_state["source_head"] = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], env=scrubbed_git_env(), text=True
    ).strip()
    return runtime


@pytest.fixture(autouse=True)
def existing_adapter_replay(
    request, tmp_path, tmp_path_factory, existing_replay_runtime
):
    with pytest.MonkeyPatch.context() as patch:
        yield from replay_case(request, patch, tmp_path, tmp_path_factory, existing_replay_runtime)


def replay_case(request, monkeypatch, tmp_path, tmp_path_factory, existing_replay_runtime):
    runtime = existing_replay_runtime
    owned = tmp_path_factory.getbasetemp().resolve()
    sidecar = request.config._codex_replay_state["sidecar"]
    home = request.config._codex_replay_state.get("owned_home", sidecar / "home")
    operation = owned / "operation"
    operation.mkdir(exist_ok=True)
    deferred = []
    monkeypatch.syspath_prepend(str(ROOT / "scripts/hooks"))
    adapter = private_module(
        "existing_replay_adapter", ROOT / "scripts/hooks/codex_validator_adapter.py"
    )
    adapter.RUNTIME_ROOT = runtime
    original_run = subprocess.run
    reducer = adapter.child_result
    remote_family = request.module.__name__ == "tests.test_hooks.test_worktree_remote_guard"
    force_policy = None
    if remote_family:
        force_policy = getattr(request.config, "_remote_force_policy", None)
        if force_policy is None:
            force_policy = private_module("remote_probe_force_policy", ROOT / "scripts/hooks/git_push_guard.py")
            request.config._remote_force_policy = force_policy
    original_repo_cwd = None
    if (request.module.__name__ == "tests.test_hooks.test_guard_ansic_fail_closed"
            and getattr(getattr(request.node, "cls", None), "__name__", None)
            == "TestNetDoesNotDowngradeHardBlocks"):
        # This class checks command-only refusal precedence. Let its helper
        # derive payload/process cwd and the git ceiling from one owned root.
        original_repo_cwd = request.module._REPO
        monkeypatch.setattr(request.module, "_REPO", str(operation))
    if request.node.originalname == "test_remove_own_cwd_blocked":
        # Preserve the property before the old test derives its own target.
        monkeypatch.chdir(tmp_path)

    def safe_path(value):
        path = Path(value).expanduser().resolve()
        return any(path.is_relative_to(root) for root in (owned, sidecar, home))

    def run(args, *positional, _deferred=False, **kwargs):
        argv = shlex.split(args) if isinstance(args, str) and kwargs.get("shell") else args
        if not isinstance(argv, (list, tuple)) or len(argv) < 2:
            return original_run(args, *positional, **kwargs)
        inline = list(argv) == shlex.split(adapter.INLINE_BASH_SAFETY)
        guard = None if inline else Path(str(argv[1]))
        if guard is not None and not guard.is_absolute():
            guard = Path(kwargs.get("cwd") or os.getcwd()) / guard
        if not inline and (guard.stem not in NAMES or guard.suffix != ".py"):
            if guard.suffix == ".py":
                ROWS.append({"nodeid": request.node.nodeid, "guard": guard.stem,
                             "kind": "direct", "reason":
                             "Unselected Python entry point outside PR1 guard set: " + guard.name})
            elif (request.module.__name__ == "tests.test_hooks.test_inline_hooks"
                    and Path(str(argv[0])).name == "bash"):
                ROWS.append({"nodeid": request.node.nodeid, "guard": "other-inline",
                             "kind": "direct", "reason": (
                                 "Bash syntax-check mode is not a tool event" if "-n" in argv
                                 else "Unselected inline/WebFetch hook is outside PR1 Bash definition")})
            return original_run(args, *positional, **kwargs)
        row = {
            "nodeid": request.node.nodeid,
            "guard": "inline_bash_safety" if inline else guard.stem,
            "kind": "unmapped",
            "reason": "",
            "original_guard_sha256": (hashlib.sha256(argv[2].encode()).hexdigest() if inline
                                      else sha(guard) if guard.is_file() else None),
            "original_argv": list(argv),
        }
        ROWS.append(row)
        if original_repo_cwd is not None:
            row.update(original_cwd=original_repo_cwd,
                       fixture_adaptation="source-reviewed command-only refusal class in owned cwd")
        if not inline and len(argv) != 2:
            row["reason"] = "Direct guard CLI mode, not a Codex hook event: " + shlex.join(argv[2:])
            return original_run(args, *positional, **kwargs)
        if not inline and direct_publishing_allowance(request, guard):
            # Owner-approved exception: the opener defeats publishing auto-allow.
            # This guard writes cache even before ASK; never baseline-pair here.
            row.update(kind="direct", reason="Owner-approved publishing allowance; separate independent-state proof",
                       publishing_test_sha256=(
                           PUBLISHING_TEST_SHA256))
            return original_run(args, *positional, **kwargs)
        if (
            not inline and guard.stem in {"secrets_env_access_guard", "git_push_guard"}
            and kwargs.get("env", {}).get("GENESIS_CC_SESSION") == "1"
        ):
            row["reason"] = (
                "Claude dispatched-session branch is not a Codex validator event"
            )
            return original_run(args, *positional, **kwargs)
        raw_input = kwargs.get("input")
        try:
            payload = json.loads(raw_input)
        except (TypeError, ValueError):
            row["reason"] = "malformed input is not a compatible hook payload"
            return original_run(args, *positional, **kwargs)
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_input"), dict):
            row["reason"] = "non-object hook input"
            return original_run(args, *positional, **kwargs)
        if (
            payload.get("tool_name", "Bash") != "Bash"
            or payload.get("hook_event_name", "PreToolUse") != "PreToolUse"
        ):
            row["reason"] = "Claude file/read/PostToolUse shape is not a native Bash payload"
            return original_run(args, *positional, **kwargs)
        if not inline and guard.stem == "main_checkout_guard":
            row["reason"] = "main-checkout Bash half is explicitly excluded by PR1"
            return original_run(args, *positional, **kwargs)
        copied_fixture = not inline and guard.parent.resolve() != (ROOT / "scripts/hooks").resolve()
        if copied_fixture:
            assert safe_path(guard) and guard.is_file() and not guard.is_symlink(), guard
            row.update(fixture_manifest(guard))
        command = payload["tool_input"].get("command")
        if not isinstance(command, str) or not command.strip():
            row["reason"] = "empty or non-string command"
            return original_run(args, *positional, **kwargs)
        actual_cwd = Path.cwd()
        default_cwd = actual_cwd if safe_path(actual_cwd) else operation
        cwd = Path(payload.get("cwd") or kwargs.get("cwd") or default_cwd)
        if (cwd.resolve() == ROOT and request.module.__name__ in {
                "tests.test_hooks.test_discarded_write", "tests.test_scripts.test_hook_input_contract"}):
            row["original_cwd"] = str(cwd)
            row["fixture_adaptation"] = "source-reviewed repository-independent payload in owned cwd"
            cwd = operation
        if cwd.is_absolute() and safe_path(cwd) and not cwd.exists():
            # These existing lexical-cwd fixtures never created their directories.
            # Codex anchors require them to exist; materialize only owned scratch.
            cwd.mkdir(parents=True)
            row["fixture_adaptation"] = "materialized owned lexical cwd"
        if not cwd.is_absolute() or not cwd.is_dir() or not safe_path(cwd):
            row["reason"] = "virtual or non-owned operation cwd needs fixture adaptation"
            return original_run(args, *positional, **kwargs)
        env = dict(kwargs.get("env") if kwargs.get("env") is not None else os.environ)
        env.setdefault("HOME", str(home))
        env.setdefault("GENESIS_HOME", str(Path(env["HOME"]) / ".genesis"))
        env.setdefault("GENESIS_DB_PATH", str(tmp_path / "absent-replay.db"))
        env.setdefault("TMPDIR", str(sidecar / "temp"))
        if env["TMPDIR"] == request.config._codex_replay_state["ambient_tmpdir"]:
            # The install exports a general temp root. Narrow only this child
            # to its owned fixture; explicit fixture-specific paths stay exact.
            env["TMPDIR"] = str(sidecar / "temp")
            row["ambient_temp_adaptation"] = "inherited TMPDIR narrowed to owned child temp"
        for key in ("HOME", "GENESIS_HOME", "GENESIS_DB_PATH", "TMPDIR"):
            assert safe_path(env[key]), f"Replay refused non-scratch {key}"
        child = None if inline else guard.resolve() if copied_fixture else runtime / "scripts/hooks" / guard.name
        script = None if inline else os.path.relpath(child, runtime / "scripts")
        if request.node.originalname == "test_refusal_runs_no_subprocess_first" and not _deferred:
            # The guard-only marker assertion precedes the launcher's own git
            # discovery. Capture immutable per-call inputs, then check that
            # separate layer after the original assertions have completed.
            snapshot = dict(kwargs)
            snapshot.update(env=env.copy(), input=json.dumps(payload), cwd=str(cwd))
            direct = original_run(args, *positional, **kwargs)
            # Replay the resolved original target, not a relative spelling
            # reinterpreted later from the normalized operation cwd.
            deferred.append(([argv[0], str(guard)], positional, snapshot, child, sha(child), row, direct))
            row.update(kind="deferred", reason="guard-only no-spawn invariant retained directly",
                       captured_argv=list(argv), captured_env_sha256=hashlib.sha256(
                           json.dumps(env, sort_keys=True).encode()).hexdigest(),
                       captured_path=env.get("PATH"), captured_cwd=str(cwd),
                       captured_payload=payload)
            return direct
        call_runtime = runtime
        mapping = "remote-policy" if remote_family else MAPPINGS.get(request.node.originalname)
        if (request.node.originalname == "test_matrix_inert_mention_is_refused_actionably_never_prompted"
                and request.node.callspec.params["ctx_name"] == "in_quoted_heredoc"
                and not inline and guard.stem == "git_push_guard"):
            mapping = "compound-heredoc-push"
        baseline = None
        if mapping:
            baseline = original_run(
                list(argv) if inline else [str(call_runtime / ".claude/hooks/genesis-hook"), script],
                input=raw_input,
                capture_output=True,
                text=True,
                cwd=cwd,
                env=env,
                timeout=8,
            )
            row.update(premise_mapping=mapping, baseline_code=baseline.returncode)
        base_specific = {}
        base_allowed = base_force = expected_force = False
        force_markers = (
            "BLOCKED: Force push to origin/",
            "BLOCKED: a force push off the public repo must be the whole command",
            "BLOCKED: force push rewrites remote history",
        )
        if mapping == "remote-policy":
            decoded_base = json.loads(baseline.stdout) if baseline.stdout.strip() else {}
            base_specific = decoded_base.get("hookSpecificOutput", {})
            base_allowed = (baseline.returncode == 0 and decoded_base.get("decision") != "block"
                            and base_specific.get("permissionDecision") not in {"ask", "deny"})
            segments, _ = force_policy.analyze_checked(command)
            expected_force = any(force_policy.git_subcommand(s.argv) == "push"
                                 and force_policy._push_is_force(s.argv) for s in segments)
            base_force = (baseline.stderr.startswith(force_markers)
                          or base_specific.get("permissionDecisionReason", "").startswith("FORCE push detected"))
            assert base_force == expected_force, "Source force census disagrees with baseline"
            if expected_force:
                assert not base_allowed, "Baseline silently allowed a source force case"
            row.update(expected_force=expected_force, baseline_force=base_force,
                       baseline_allowed=base_allowed, baseline_decision=base_specific.get("permissionDecision"),
                       original_command=command)
        data = {
            **payload,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(cwd),
            "tool_input": {
                **payload["tool_input"],
                "command": "cd " + shlex.quote(str(cwd)) + " || exit\n" + command,
            },
        }
        row.update(
            kind="replay",
            reason="",
            cwd=str(cwd),
            input_sha256=hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
            copied_guard_sha256=(hashlib.sha256(argv[2].encode()).hexdigest() if inline else sha(child)),
            copied_launcher_sha256=sha(call_runtime / ".claude/hooks/genesis-hook"),
            actual_target="inline_bash_safety" if inline else str(child),
        )
        assert row["copied_guard_sha256"] == row["original_guard_sha256"]
        captured = {}

        def observe(code, stdout, stderr):
            captured.update(code=code, stdout=stdout, stderr=stderr)
            return reducer(code, stdout, stderr)

        saved = None if inline else child.read_bytes()
        fault = os.environ.get("ADAPTER_REPLAY_CHILD_FAULT")
        if mapping == "remote-policy" and not expected_force:
            fault = None
        try:
            if fault:
                assert fault in ("empty", "deny")
                if child is not None:
                    child.write_text("pass\n" if fault == "empty" else
                                     'import sys; print("BLOCKED: injected child denial", file=sys.stderr); sys.exit(2)\n')
                injected_inline = ":" if fault == "empty" else "echo 'BLOCKED: injected child denial' >&2; exit 2"
                row.update(child_fault=fault, injected_guard_sha256=(sha(child) if child else
                           hashlib.sha256(injected_inline.encode()).hexdigest()))
                assert row["injected_guard_sha256"] != row["copied_guard_sha256"]
            with monkeypatch.context() as scoped:
                scoped.setattr(adapter, "RUNTIME_ROOT", call_runtime)
                call = next(c for c in adapter.prepare(data) if (
                    c[0] == shlex.split(adapter.INLINE_BASH_SAFETY) if inline
                    else c[0] == [str(call_runtime / ".claude/hooks/genesis-hook"), "hooks/" + guard.name]))
                row["prepared_argv"] = list(call[0])
                if copied_fixture:
                    call = ([call[0][0], script], call[1], call[2])
                if inline and fault:
                    call = (["bash", "-c", injected_inline], call[1], call[2])
                row["actual_argv"] = list(call[0])
                assert call[1]["tool_input"]["command"] == data["tool_input"]["command"]
                # An exact replacement retains even intentionally minimal fixture
                # envs; run_guard owns its documented child modifications.
                scoped.setattr(os, "environ", env)
                scoped.setattr(adapter, "child_result", observe)
                try:
                    note = asyncio.run(adapter.run_guard(*call))
                except adapter.Refused as exc:
                    allowed, note = False, str(exc)
                else:
                    allowed = True
        finally:
            if fault and child is not None:
                child.write_bytes(saved)
            if child is not None:
                assert sha(child) == row["copied_guard_sha256"], "Executed guard source changed"
        assert captured, "Adapter did not execute the real guard/reducer boundary"
        out, err, code = captured["stdout"], captured["stderr"], captured["code"]
        specific = {}
        legacy_block = False
        if out.strip():
            decoded = json.loads(out)
            specific = decoded.get("hookSpecificOutput", {})
            legacy_block = decoded.get("decision") == "block"
        expected = (
            code == 0
            and not legacy_block
            and specific.get("permissionDecision") not in ("ask", "deny")
        )
        row.update(
            raw_code=code,
            raw_decision=specific.get("permissionDecision"),
            raw_context=bool(specific.get("additionalContext")),
            actual_allow=allowed,
            expected_allow=expected,
        )
        assert allowed == expected, "Adapter admission disagrees with actual child result"
        if allowed:
            assert specific.get("permissionDecisionReason", "") in note
            assert specific.get("additionalContext", "") in note
            assert err.decode(errors="replace").strip() in note
        elif code == 2 and err.strip():
            assert err.decode(errors="replace").strip() in note
        elif specific.get("permissionDecision") in ("ask", "deny"):
            assert specific.get("permissionDecisionReason", "") in note
        if mapping == "remote-policy":
            assert allowed == base_allowed, "Paired remote policy changed admission"
            if expected_force:
                assert code == 2 and err.decode(errors="replace").startswith(force_markers), "Paired force reason lost"
            else:
                assert code == baseline.returncode, "Paired remote policy changed raw code"
                assert specific.get("permissionDecision") == base_specific.get("permissionDecision"), "Paired remote policy changed permission"
                assert baseline.stderr.strip() in note, "Paired remote explanation lost"
                assert base_specific.get("additionalContext", "") in note, "Paired remote context lost"
                assert (base_specific.get("permissionDecisionReason") or "") in note, "Paired remote approval reason lost"
                assert (decoded_base.get("reason") or "") in note, "Paired remote legacy reason lost"
        elif mapping == "known-cwd":
            assert baseline.returncode == 2 and allowed
        elif mapping == "compound-heredoc":
            assert baseline.returncode == 0 and not allowed
            assert "stdin or a list" in note
        elif mapping == "compound-note":
            marker = "the ENTIRE command was discarded"
            assert baseline.returncode == 2 and not allowed
            assert marker not in baseline.stderr and marker in note
        elif mapping == "compound-heredoc-push":
            assert baseline.returncode == 0 and not allowed
            assert "write the text to a file instead" in note
        elif mapping == "graphql-file":
            assert not allowed and code == 2
            assert "query this guard cannot read" in note
            assert baseline.returncode in (0, 2)
            if baseline.returncode == 2:
                assert "through the GitHub API" in baseline.stderr
            row["approved_behavior_change"] = baseline.returncode == 0
        row["adapter_assertions_passed"] = True
        if baseline is not None:
            return baseline
        text_mode = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
        result = subprocess.CompletedProcess(
            args, code, out.decode() if text_mode else out, err.decode() if text_mode else err
        )
        if kwargs.get("check"):
            result.check_returncode()
        return result

    monkeypatch.setattr(subprocess, "run", run)
    module = request.module
    if module.__name__ == "tests.test_hooks.test_full_suite_guard":
        classify = module._mod._targets_specific_test

        def argv_classifier(args):
            result = classify(args)
            if inspect.currentframe().f_back.f_code.co_filename == module.__file__:
                proc = run(
                    [sys.executable, str(ROOT / "scripts/hooks/full_suite_guard.py")],
                    input=json.dumps({"tool_input": {"command": shlex.join(["pytest", *args])}}),
                    capture_output=True,
                    text=True,
                )
                assert (proc.returncode == 0) == result
            return result

        monkeypatch.setattr(module._mod, "_targets_specific_test", argv_classifier)
    monkeypatch.setattr(subprocess, "run", run)
    try:
        yield
        for argv, positional, kwargs, child, digest, row, baseline in deferred:
            assert sha(child) == digest, "Deferred guard source changed before its check"
            result = run(argv, *positional, _deferred=True, **kwargs)
            replay = ROWS[-1]
            decoded = json.loads(baseline.stdout) if baseline.stdout.strip() else {}
            specific = decoded.get("hookSpecificOutput", {})
            baseline_allow = (baseline.returncode == 0 and decoded.get("decision") != "block"
                              and specific.get("permissionDecision") not in {"ask", "deny"})
            assert replay["actual_allow"] == baseline_allow, "Deferred adapter lost direct admission oracle"
            marker = "through the GitHub API"
            if marker in baseline.stderr:
                assert marker in result.stderr, "Deferred adapter lost API refusal marker"
            row.update(kind="direct-layer", reason="guard-only no-spawn baseline; adapter replay recorded separately")
    finally:
        monkeypatch.undo()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    phase = {"outcome": report.outcome}
    if report.skipped:
        phase["skip_reason"] = (
            report.longrepr[2] if isinstance(report.longrepr, tuple) else "skipped"
        )
    if getattr(report, "wasxfail", None):
        phase["xfail"] = report.wasxfail
    REPORTS.setdefault(item.nodeid, {})[report.when] = phase


def pytest_collection_finish(session):
    for item in session.items:
        REPORTS.setdefault(item.nodeid, {})
        SOURCE_ITEMS[item.nodeid] = {
            "file": item.location[0],
            "line": item.location[1] + 1,
            "symbols": sorted(set(item.function.__code__.co_names)),
        }


def pytest_sessionfinish(session, exitstatus):
    coverage = {}
    reports = {}
    for row in ROWS:
        if row.get("child_fault") or row.get("injected_guard_sha256"):
            row.update(kind="instrument-control",
                       reason="Intentional child fault: instrument control, no unchanged-source qualification")
    final_hash = sha(Path(__file__))
    source_mismatches = []
    runtime = getattr(session.config, "_codex_replay_runtime", None)
    state = getattr(session.config, "_codex_replay_state", {})
    source_head = state.get("source_head")
    if runtime is not None:
        for relative, digest in COPIED.items():
            for root in (ROOT, runtime):
                path = root / relative
                if not path.is_file() or sha(path) != digest:
                    source_mismatches.append(str(path))
        for row in ROWS:
            for relative, digest in row.get("public_source_references", {}).items():
                if not (ROOT / relative).is_file() or sha(ROOT / relative) != digest:
                    source_mismatches.append(str(ROOT / relative))
        if source_head != subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], env=scrubbed_git_env(), text=True
        ).strip():
            source_mismatches.append("source HEAD changed")
    instrument_unchanged = final_hash == INSTRUMENT_SHA256
    unchanged = instrument_unchanged and not source_mismatches
    if not unchanged:
        exitstatus = session.exitstatus = pytest.ExitCode.INTERRUPTED
    for nodeid in SOURCE_ITEMS:
        phases = REPORTS.get(nodeid, {})
        outcomes = [phase["outcome"] for phase in phases.values()]
        result = (
            "failed"
            if "failed" in outcomes
            else "skipped"
            if "skipped" in outcomes
            else "not-run"
            if set(phases) != {"setup", "call", "teardown"}
            else "xpassed"
            if any(phase.get("xfail") for phase in phases.values())
            else "passed"
        )
        reports[nodeid] = result
        rows = [r for r in ROWS if r["nodeid"] == nodeid]
        replayed = sum(
            r["kind"] == "replay" and r.get("adapter_assertions_passed", False) for r in rows
        )
        status = "FULL" if rows and replayed == len(rows) else "PARTIAL" if replayed else "NO"
        coverage[nodeid] = {
            "status": status,
            "invocations": len(rows),
            "completed_replays": replayed,
            "qualified_replays": replayed if result == "passed" and unchanged else 0,
            "direct_test_result": result,
            **SOURCE_ITEMS[nodeid],
            "exclusions": [r["reason"] for r in rows if r["kind"] != "replay"]
            or (
                []
                if replayed
                else ["Internal/helper/mock or registration invariant; no subprocess hook payload"]
            ),
        }
    Path(session.config.getoption("--codex-replay-receipt")).write_text(
        json.dumps(
            {
                "exitstatus": exitstatus,
                "reports": reports,
                "phases": REPORTS,
                "coverage": coverage,
                "rows": ROWS,
                "copied_source_hashes": COPIED,
                "instrument_sha256": INSTRUMENT_SHA256,
                "instrument_final_sha256": final_hash,
                "instrument_unchanged": instrument_unchanged,
                "provenance_valid": unchanged,
                "source_head": source_head,
                "source_mismatches": sorted(set(source_mismatches)),
                "owned_fixture_roots": {
                    key: str(value) for key, value in
                    getattr(session.config, "_codex_replay_state", {}).items()
                    if key in {"base", "sidecar", "owned_home"}
                },
            },
            indent=2,
        )
    )
