"""Host-safe bridge boundaries; real monolith/native composition runs only in CI."""

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import aiosqlite
import pytest

from genesis.db.schema import TABLES

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    result = []
    for name in ("headlong_native_controls", "headlong_native_bridge"):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/ci/{name}.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: module}):
            spec.loader.exec_module(module)
        result.append(module)
    return result


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(tmp_path / "fixture.sqlite3") as writer:
        writer.row_factory = aiosqlite.Row
        for name in ("ego_proposals", "ego_proposal_revisions"):
            await writer.execute(TABLES[name])
        yield writer


async def test_receipt_and_observation_follow_actual_effect(modules, db, tmp_path):
    native, bridge = modules
    await native.prepare_case(db, "approved")
    observed = []

    def observation():
        observed.append((tmp_path / "effect.bin").read_bytes())

    receipt = tmp_path / "calls.jsonl"
    port = native.EffectPort(db, tmp_path, hooks=native.EffectHooks(receipt, observation))
    await port.call({"proposal_id": "proposal"})
    await port.call({"proposal_id": "proposal"})
    assert observed == [native.EFFECT]
    assert bridge.ReceiptView(receipt).calls == port.calls
    assert [row["claim"] for row in port.calls] == [True, False]


async def test_effect_complete_observation_failure_is_recorded(modules, db, tmp_path):
    native, bridge = modules
    await native.prepare_case(db, "approved")

    def observation():
        raise OSError("synthetic observation failure")

    receipt = tmp_path / "calls.jsonl"
    port = native.EffectPort(db, tmp_path, hooks=native.EffectHooks(receipt, observation))
    with pytest.raises(OSError, match="synthetic observation"):
        await port.call({"proposal_id": "proposal"})
    assert (tmp_path / "effect.bin").read_bytes() == native.EFFECT
    assert port.calls[0]["claim"] is True and port.calls[0]["outcome"] == "error"
    assert bridge.ReceiptView(receipt).calls == port.calls


@pytest.mark.parametrize("phase", ["open", "short", "close"])
async def test_receipt_failure_is_error_even_after_effect(modules, db, tmp_path, monkeypatch, phase):
    native, _ = modules
    await native.prepare_case(db, "approved")
    receipt = tmp_path / "calls.jsonl"
    original = Path.open

    class FailedReceipt:
        def __enter__(self):
            self.stream = original(receipt, "a")
            return self

        def write(self, text):
            return self.stream.write(text[:3] if phase == "short" else text)

        def __exit__(self, *args):
            self.stream.close()
            if phase == "close":
                raise OSError("synthetic receipt close failure")

    def failed_open(path, *args, **kwargs):
        if path == receipt:
            if phase == "open":
                raise OSError("synthetic receipt failure")
            return FailedReceipt()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failed_open)
    port = native.EffectPort(db, tmp_path, hooks=native.EffectHooks(receipt))
    with pytest.raises(OSError):
        await port.call({"proposal_id": "proposal"})
    assert port.calls[0]["claim"] is True and port.calls[0]["outcome"] == "error"
    assert (tmp_path / "effect.bin").read_bytes() == native.EFFECT


@pytest.mark.parametrize("text", ['{"outcome":"refused"}', '{broken\n', '[]\n', '{}\n'])
def test_partial_or_malformed_receipt_is_never_a_refusal(modules, tmp_path, text):
    _, bridge = modules
    receipt = tmp_path / "calls.jsonl"
    receipt.write_text(text)
    with pytest.raises(ValueError):
        _ = bridge.ReceiptView(receipt).calls


def test_missing_receipt_has_no_handler_calls(modules, tmp_path):
    _, bridge = modules
    assert bridge.ReceiptView(tmp_path / "absent.jsonl").calls == []


@pytest.fixture
def bridge_request(tmp_path):
    identity = tmp_path / "identity"
    (identity / "run").mkdir(parents=True)
    (identity / "workdir").mkdir()
    prompt = identity / "run/monolith_prompt.fixture"
    prompt.write_text("whole Unicode context: λ\n" + "context payload " * 10000)
    config = {"root": str(tmp_path), "identity_dir": str(identity), "traj_id": "fixture-traj", "model": "sonnet"}
    argv = ["--model", "sonnet", "--traj", "fixture-traj", "--workdir", str(identity / "workdir"),
            "--prompt-file", str(prompt)]
    return config, argv, prompt


def test_file_prompt_preserves_large_unicode_context(modules, bridge_request):
    _, bridge = modules
    config, argv, prompt = bridge_request
    parsed = bridge.parse_request(argv, config)
    assert len(parsed.prompt.encode()) > 131072
    assert parsed.prompt == prompt.read_text()


@pytest.mark.parametrize("flag,value", [("--model", "unexpected"), ("--traj", "foreign"),
                                       ("--workdir", "/tmp"), ("--prompt-file", "owned-outside-prompt")])
def test_bridge_rejects_mismatched_fixture_inputs(modules, bridge_request, flag, value):
    _, bridge = modules
    config, argv, _ = bridge_request
    if value == "owned-outside-prompt":
        outside = Path(config["root"]) / "outside.txt"
        outside.write_text("synthetic outside context")
        value = str(outside)
    argv[argv.index(flag) + 1] = value
    with pytest.raises(ValueError):
        bridge.parse_request(argv, config)


def test_bridge_does_not_silently_accept_legacy_flags(modules, bridge_request):
    _, bridge = modules
    config, argv, _ = bridge_request
    with pytest.raises(SystemExit):
        bridge.parse_request(argv + ["--bin", "/headlong/bin/shellm"], config)


def test_wake_check_separates_zero_and_five_second_delays(modules):
    _, bridge = modules
    # A 12-second invocation would make broad start/end windows overlap.
    assert bridge.wake_matches(117, 112.1, 5)
    assert not bridge.wake_matches(112, 112.1, 5)
    assert bridge.wake_matches(112, 112.1, 0)
    assert not bridge.wake_matches(117, 112.1, 0)


@pytest.mark.parametrize("status,expected", [("0", None), ("65", "reference_conflict"), (None, "sanitizer_failure"),
                                          ("3", "sanitizer_failure"), ("5", "sanitizer_failure"),
                                          ("127", "sanitizer_failure"), ("bad", "sanitizer_failure")])
def test_prompt_errors_are_typed_before_native_dispatch(modules, status, expected):
    _, bridge = modules
    assert bridge.prompt_failure(status) == expected


async def test_native_query_preserves_context_without_file_expansion(modules, tmp_path, monkeypatch):
    native, _ = modules
    sdk, captured = ModuleType("claude_agent_sdk"), {}

    @dataclass
    class Result:
        result: str = "synthetic final"
        session_id: str = "synthetic-session"

    class Options:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class Client:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, prompt):
            captured["query"] = prompt

        async def receive_response(self):
            yield Result()

    sdk.ClaudeAgentOptions, sdk.ClaudeSDKClient, sdk.ResultMessage = Options, Client, Result
    sdk.create_sdk_mcp_server = lambda **kwargs: {"type": "fixture"}
    sdk.tool = lambda *args: lambda function: function
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    cwd = tmp_path / "identity/workdir"
    cwd.mkdir(parents=True)
    prompt = "whole λ context @mention-target.txt\n" + "context payload " * 10000
    result, _, _ = await native.native_turn(native.NativeFixture(tmp_path, "echo", None,
        SimpleNamespace(server=SimpleNamespace(server_port=12345)), Path("/never-executed"),
        prompt=prompt, model="haiku", cwd=cwd), native.NativeTrace())
    assert result["result"] == "synthetic final"
    assert captured["query"] == prompt and captured["cwd"] == str(cwd)
    assert captured["model"] == "haiku"
    assert captured.get("verbatim_prompts") is True


@pytest.fixture
def controls(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    name = "headlong_monolith_controls"
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/ci/{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


def test_patch_rejects_missing_and_ambiguous_invocations(controls):
    patch = {"before": "original", "after": "native"}
    assert controls.patched_source("prefix original suffix", patch) == "prefix native suffix"
    for text in ("missing", "original original"):
        with pytest.raises(ValueError):
            controls.patched_source(text, patch)


def test_patch_requires_both_redaction_and_invocation_regions(controls):
    patch = {"before": "original", "after": "native", "prompt_before": "unsafe", "prompt_after": "guarded"}
    assert controls.patched_source("prefix unsafe original suffix", patch) == "prefix guarded native suffix"
    for text in ("prefix original suffix", "unsafe unsafe original"):
        with pytest.raises(ValueError):
            controls.patched_source(text, patch)


async def test_curated_child_env_preserves_source_import_without_host_secrets(controls, tmp_path, monkeypatch):
    captured = {}

    class ProbeStopped(RuntimeError):
        pass

    def capture_before_execution(argv, env, *args, **kwargs):
        captured.update(env)
        raise ProbeStopped

    monkeypatch.setenv("UNRELATED_FIXTURE_TOKEN", "synthetic-do-not-forward")
    monkeypatch.setattr(controls, "command", capture_before_execution)
    # Stop at the first child boundary: no upstream or native process executes.
    with pytest.raises(ProbeStopped):
        await controls.run_scenario(tmp_path / "owned-case", "effect")
    assert captured["PYTHONPATH"] == "/genesis-src"
    assert "UNRELATED_FIXTURE_TOKEN" not in captured


@pytest.fixture
def successful_run():
    call = {"canonical_called": True, "claim": True, "outcome": "executed"}
    terminal = {"is_error": False, "subtype": "success", "result": "native-final-approved", "session_id": "native-a"}
    receipt = "fixture receipt proposal=proposal effect=" + hashlib.sha256(b"native-fixture-effect\n").hexdigest()
    prompt = "fixture-identity-marker fixture-goal-marker fixture-skill-marker fixture-memory-marker fixture-stream-marker @mention-target.txt"
    return {"driver": {"complete": True, "run_id": "run-a", "model": "sonnet", "native": terminal, "calls": [call],
                       "prompt_status": "0", "failure_kind": None, "invocation_id": "fixture-nonce", "trigger_id": "wake-a",
                       "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                       "observation_id": "obs-a", "final_id": "final-a", "error": None,
                       "messages": [{"type": "ResultMessage", "data": terminal},
                                    {"type": "SystemMessage", "data": {"subtype": "init", "data": {"tools": ["mcp__fixture__claim_effect"]}}}]},
            "backend": {"main_requests": 2, "unexpected": [], "provider_error": False, "tool_results": ["toolu_fixture_1"],
                        "captures": [{"body": {"messages": [{"role": "user", "content": prompt}]}}]},
            "raw_prompt": prompt, "receipt_calls": [call.copy()], "state": {"status": "executed"},
            "invocation_id": "fixture-nonce", "prompt_captured": True,
            "effect": "native-fixture-effect\n", "backoff": {"level": 0}, "wake_at": 100, "wake_mtime": 100.1,
            "start": 99, "end": 101, "step_returncode": 0,
            "trigger": {"type": "monolith-wake", "step_id": "wake-a", "source": "monolith-timer"},
            "trajectory": [{"type": "shellm-run", "step_id": "run-a", "source": "monolith", "engine": "claude-code-sdk",
                            "wake": "spontaneous/timer", "trigger_step": "wake-a", "model": "sonnet", "invocation_id": "fixture-nonce"},
                           {"type": "observation", "step_id": "obs-a", "source": "monolith", "run_id": "run-a", "content": receipt},
                           {"type": "final", "step_id": "final-a", "source": "monolith", "run_id": "run-a",
                            "native_session_id": "native-a", "content": receipt + "\nnative-final-approved"}]}


def test_bound_effect_and_native_evidence_passes(controls, successful_run):
    assert controls.qualify_run("effect", successful_run)


@pytest.mark.parametrize("defect", ["lost_observation", "lost_final", "wrong_state", "extra_effect", "receipt_error",
                                  "unexpected_request", "lost_context", "wrong_engine", "wrong_wake", "wrong_model", "native_error",
                                  "wrong_nonce", "bad_prompt_status"])
def test_missing_or_inconsistent_bridge_evidence_cannot_pass(controls, successful_run, defect):
    row = successful_run
    if defect == "lost_observation":
        row["trajectory"] = [step for step in row["trajectory"] if step["type"] != "observation"]
    elif defect == "lost_final":
        row["driver"]["final_id"] = None
    elif defect == "wrong_state":
        row["state"]["status"] = "approved"
    elif defect == "extra_effect":
        row["effect"] *= 2
    elif defect == "receipt_error":
        row["receipt_calls"][0]["outcome"] = "error"
    elif defect == "unexpected_request":
        row["backend"]["unexpected"] = ["unclassified"]
    elif defect == "lost_context":
        row["backend"]["captures"] = []
    elif defect == "wrong_engine":
        row["trajectory"][0]["engine"] = "shellm"
    elif defect == "wrong_wake":
        row["wake_at"] = 105
    elif defect == "wrong_model":
        row["driver"]["model"] = "haiku"
    elif defect == "wrong_nonce":
        row["driver"]["invocation_id"] = "other"
    elif defect == "bad_prompt_status":
        row["driver"]["prompt_status"] = "65"
    else:
        row["driver"]["native"]["is_error"] = True
    assert not controls.qualify_run("effect", row)


@pytest.fixture
def conflict_run(successful_run):
    row = successful_run
    row["driver"].update(complete=False, native=None, messages=[], stderr=[], calls=[], observation_id=None,
                         final_id=None, error="ValueError: reference_conflict", prompt_status="65",
                         prompt_sha256=hashlib.sha256(b"").hexdigest(),
                         failure_kind="reference_conflict", invocation_id="fixture-nonce", trigger_id="wake-a")
    row.update(raw_prompt="", receipt_calls=[], effect=None, state={"status": "approved"},
               state_before={"status": "approved"}, invocation_id="fixture-nonce", backoff={"level": 1}, wake_at=105)
    row["backend"].update(main_requests=0, tool_results=[], captures=[])
    header = row["trajectory"][0]
    header["invocation_id"] = "fixture-nonce"
    row["trajectory"] = [header, {"type": "error", "source": "monolith", "run_id": "run-a",
                                  "trigger_step": "wake-a", "rc": 2, "reason": "run-failed"}]
    return row


def test_explicit_blocked_prompt_control_passes_without_sdk(controls, conflict_run):
    assert controls.qualify_run("reference_conflict", conflict_run)


@pytest.mark.parametrize("defect", ["compile_failure", "provider_request", "effect", "claimed", "lost_header",
                                  "wrong_nonce", "missing_error", "wrong_timer", "missing_capture"])
def test_blocked_prompt_control_rejects_unrelated_failure_or_action(controls, conflict_run, defect):
    row = conflict_run
    if defect == "compile_failure":
        row["driver"]["prompt_status"] = "3"
    elif defect == "provider_request":
        row["backend"]["main_requests"] = 1
    elif defect == "effect":
        row["effect"] = "native-fixture-effect\n"
    elif defect == "claimed":
        row["state"] = {"status": "executed"}
    elif defect == "lost_header":
        row["trajectory"] = row["trajectory"][1:]
    elif defect == "wrong_nonce":
        row["driver"]["invocation_id"] = "other"
    elif defect == "missing_error":
        row["trajectory"] = row["trajectory"][:1]
    elif defect == "missing_capture":
        row["prompt_captured"] = False
    else:
        row["wake_at"] = 100
    assert not controls.qualify_run("reference_conflict", row)


@pytest.fixture
async def entrypoint(modules, tmp_path, monkeypatch):
    """Real owned config/CRUD/main; fake SDK and trajectory boundary, no processes."""
    from headlong_prompt_references import publish_config

    _, bridge = modules
    native = bridge.native
    root, records, calls = tmp_path / "case", [], []
    identity, run = root / "identity", root / "run-0"
    for path in (identity / "run", identity / "workdir", run):
        path.mkdir(parents=True, exist_ok=True)
    prompt = identity / "run/monolith_prompt.fixture"
    prompt.write_text("owned Unicode context λ")
    trigger = "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"
    env = publish_config({"root": str(root), "run_root": str(run), "identity_dir": str(identity), "traj_id": trigger,
                          "trigger_id": trigger, "reference_ids": [trigger], "model": "sonnet", "kind": "approved",
                          "backend_port": 12345}, {"IDENTITY_DIR": str(identity), "TRAJ_ID": trigger})
    env.update(SHELLM_TRIGGER_STEP_ID=trigger, SHELLM_WAKE="spontaneous/timer", SHELLM_RUN_ID_OUT=str(identity / "run/native-run-id"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect = aiosqlite.connect
    async with connect(root / "fixture.sqlite3") as db:
        for table in ("ego_proposals", "ego_proposal_revisions"):
            await db.execute(TABLES[table])
        await native.prepare_case(db, "approved")

    def append(record, cwd):
        identifier = f"00000000-0000-4000-8000-{len(records) + 1:012d}"
        records.append({**record, "step_id": identifier})
        return identifier

    async def turn(fixture, trace):
        calls.append("native")
        await fixture.port.call({"proposal_id": "proposal"})
        return {"is_error": False, "subtype": "success", "result": "native-final-approved", "session_id": "owned-sdk-stub"}, [], []

    sdk = ModuleType("claude_agent_sdk")
    sdk.__file__ = str(root / "owned-sdk-stub.py")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setattr(bridge, "preflight", lambda: None)
    monkeypatch.setattr(bridge, "append_step", append)
    monkeypatch.setattr(native, "native_turn", turn)
    monkeypatch.setattr(sys, "argv", ["bridge", "--model", "sonnet", "--traj", trigger, "--workdir", str(identity / "workdir"),
                                     "--prompt-file", str(prompt)])

    async def invoke(status="0"):
        if status is None:
            monkeypatch.delenv("HEADLONG_NATIVE_PROMPT_STATUS", raising=False)
        else:
            monkeypatch.setenv("HEADLONG_NATIVE_PROMPT_STATUS", status)
        if status != "0":
            prompt.write_text("")
        return await bridge.main(tmp_path)

    return SimpleNamespace(bridge=bridge, native=native, root=root, run=run, records=records, calls=calls, invoke=invoke, connect=connect)


@pytest.mark.parametrize("status", ["0", "65", "3", None])
async def test_actual_bridge_entrypoint_success_and_blocked_prompts(entrypoint, status):
    entry = entrypoint
    assert await entry.invoke(status) == (0 if status == "0" else 2)
    report = json.loads((entry.run / "driver.json").read_text())
    assert report["complete"] is (status == "0")
    assert len(entry.calls) == int(status == "0")
    assert [row["type"] for row in entry.records] == (["shellm-run", "observation", "final"] if status == "0" else ["shellm-run"])
    assert (entry.root / "effect.bin").exists() is (status == "0")
    assert report["failure_kind"] == entry.bridge.prompt_failure(status)


@pytest.mark.parametrize("phase", ["db_close", "driver_open", "driver_short", "driver_close", "rename", "stdout", "stdout_flush"])
async def test_late_failure_cannot_publish_success_or_replay(entrypoint, monkeypatch, phase):
    entry = entrypoint
    original_open, original_replace = Path.open, Path.replace

    class CloseFault:
        def __init__(self, *args, **kwargs):
            self.connection = entry.connect(*args, **kwargs)

        async def __aenter__(self):
            return await self.connection.__aenter__()

        async def __aexit__(self, *args):
            await self.connection.__aexit__(*args)
            raise OSError("synthetic database close failure")

    class StreamFault:
        def __init__(self, path, *args, **kwargs):
            self.stream = original_open(path, *args, **kwargs)

        def __enter__(self):
            return self

        def write(self, text):
            return self.stream.write(text[:3] if phase == "driver_short" else text)

        def __exit__(self, *args):
            self.stream.close()
            if phase == "driver_close":
                raise OSError("synthetic driver close failure after complete bytes")

    def faulty_open(path, *args, **kwargs):
        if path in {entry.run / "driver.json", entry.run / "driver.pending"}:
            if phase == "driver_open":
                raise OSError("synthetic driver open failure")
            return StreamFault(path, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    def faulty_replace(path, destination):
        if path == entry.run / "driver.pending":
            raise OSError("synthetic driver rename failure")
        return original_replace(path, destination)

    def faulty_output(*args, **kwargs):
        raise OSError("synthetic stdout failure")

    with monkeypatch.context() as fault:
        if phase == "db_close":
            fault.setattr(entry.bridge.aiosqlite, "connect", CloseFault)
        elif phase.startswith("driver_"):
            fault.setattr(Path, "open", faulty_open)
        elif phase == "rename":
            fault.setattr(Path, "replace", faulty_replace)
        elif phase == "stdout":
            fault.setattr(sys, "stdout", SimpleNamespace(write=faulty_output, flush=lambda: None))
        else:
            fault.setattr(sys, "stdout", SimpleNamespace(write=lambda text: len(text), flush=faulty_output))
        with pytest.raises(OSError):
            await entry.invoke()
    assert not (entry.run / "driver.json").exists()
    assert (entry.root / "effect.bin").read_bytes() == entry.native.EFFECT
    assert entry.calls == ["native"]
    assert [row["type"] for row in entry.records] == ["shellm-run", "observation", "final"]
    async with entry.connect(entry.root / "fixture.sqlite3") as db:
        db.row_factory = aiosqlite.Row
        assert (await entry.native.ego.get_proposal(db, "proposal"))["status"] == "executed"
    with pytest.raises(ValueError, match="reused"):
        await entry.invoke()
    assert entry.calls == ["native"] and (entry.root / "effect.bin").read_bytes() == entry.native.EFFECT
