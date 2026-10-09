"""Closed status projection and owned-child failures, using disposable scripts."""

import asyncio
import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_validator_serving as serving  # noqa: E402

SPEC = importlib.util.spec_from_file_location("closed_serving_request", SCRIPTS / "codex_validator_request.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
TOKEN = "b1-" + "a" * 24
VALUES = {"serving": "1" * 40, "head": "2" * 40, "mainpid": "321",
          "invocation": "3" * 32, "runtime-edits": "none", "runtime-overrides": "none",
          "bracket": TOKEN}


def output(values=None):
    return ("\n".join(f"{k}: {v}" for k, v in (VALUES if values is None else values).items()) + "\n").encode()


def runtime(tmp_path, script):
    root = tmp_path / "runtime's space"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/deploy_code_only.sh").write_text(script)
    return root


@pytest.mark.parametrize("field", sorted(VALUES))
def test_every_field_is_required_and_duplicates_refuse(field):
    with pytest.raises(ValueError):
        serving.project(output({k: v for k, v in VALUES.items() if k != field}))
    for extra in (f"{field}: {VALUES[field]}\n", f"{field}:{VALUES[field]}\n"):
        with pytest.raises(ValueError):
            serving.project(output() + extra.encode())


@pytest.mark.parametrize("field,value", [
    ("serving", "unknown (private path)"), ("head", "F" * 40), ("mainpid", "0"),
    ("mainpid", "-1"), ("mainpid", "01"), ("invocation", "unknown"),
    ("runtime-edits", "1 paths: private path"), ("runtime-overrides", "unreadable"),
    ("runtime-overrides", "0 files, " + "a" * 16), ("bracket", "unknown (private marker)"),
])
def test_unknown_and_malformed_identity_refuse(field, value):
    with pytest.raises(ValueError):
        serving.project(output({**VALUES, field: value}))


def test_only_projected_fields_escape_and_docs_head_may_differ():
    result = serving.project(output() + b"  PENDING: private/path\nexplanation: secret marker\n")
    assert result == {**VALUES, "mainpid": 321, "established": True}
    assert result["head"] != result["serving"]
    assert serving.project(output({**VALUES, "runtime-overrides": "1 files, " + "a" * 16}))["established"]
    with pytest.raises(UnicodeDecodeError):
        serving.project(output() + b"\xff")


def test_verify_uses_the_real_sentence_and_requested_token():
    result = serving.project(output({**VALUES, "bracket": serving.VALID_BRACKET}), token=TOKEN)
    assert result["bracket"] == TOKEN and result["verified"] and result["established"]
    for value in (TOKEN, "INVALID — private state", serving.VALID_BRACKET + " extra"):
        with pytest.raises(ValueError):
            serving.project(output({**VALUES, "bracket": value}), token=TOKEN)


@pytest.mark.parametrize("payload", [
    {}, {"version": True, "operation": "serving_status"},
    {"version": 1.0, "operation": "serving_status"}, {"version": 1, "operation": []},
    {"version": 1, "operation": "serving_status", "token": TOKEN},
    {"version": 1, "operation": "serving_verify"},
    *[{"version": 1, "operation": "serving_verify", "token": t} for t in (None, False, [], "b1-", TOKEN + "\n")],
    {"version": 1, "operation": "shell"},
])
def test_closed_schema_refuses_before_child(payload, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid schema entered serving observation")
    monkeypatch.setattr(serving, "observe", forbidden)
    with pytest.raises(ValueError):
        runner.execute(payload)


@pytest.mark.parametrize("operation", ["serving_status", "serving_verify"])
def test_runner_passes_only_launch_owned_root_and_exact_token(monkeypatch, operation):
    calls = []
    monkeypatch.setattr(serving, "observe", lambda root, **kwargs: calls.append((root, kwargs)) or {"established": False})
    request = {"version": 1, "operation": operation}
    if operation == "serving_verify":
        request["token"] = TOKEN
    assert runner.execute(request) == {"version": 1, "operation": operation, "established": False}
    assert calls == [(runner.RUNTIME_ROOT, {"token": TOKEN if operation == "serving_verify" else None})]


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, TOKEN])
async def test_fixed_argv_scrubbed_environment_and_full_drain(monkeypatch, tmp_path, token):
    captured = {}
    async def spawn(*argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        proc = type("Proc", (), {})()
        proc.stdout = asyncio.StreamReader()
        proc.stderr = asyncio.StreamReader()
        proc.stdout.feed_data(output())
        proc.stdout.feed_eof()
        proc.stderr.feed_data(b"private stderr")
        proc.stderr.feed_eof()
        proc.wait = AsyncMock(return_value=0)
        return proc
    monkeypatch.setattr(serving.asyncio, "create_subprocess_exec", spawn)
    for key in ("BASH_ENV", "ENV", "LD_PRELOAD", "GIT_CONFIG_COUNT", "PYTHONPATH", "GENESIS_DEPLOY_ROOT"):
        monkeypatch.setenv(key, "private marker")
    assert await serving._capture(tmp_path, token) == output()
    expected = ("/bin/bash", str(tmp_path / "scripts/deploy_code_only.sh"), "status")
    assert captured["argv"] == expected + (("--verify", token) if token is not None else ())
    assert captured["env"] == serving.child_environment()
    assert not any(k in captured["env"] for k in ("BASH_ENV", "ENV", "LD_PRELOAD", "GIT_CONFIG_COUNT", "PYTHONPATH", "GENESIS_DEPLOY_ROOT"))
    assert captured["start_new_session"] and captured["stdin"] == asyncio.subprocess.DEVNULL
    assert captured["cwd"] == tmp_path


@pytest.mark.parametrize("kind", ["known", "verify", "partial", "failure", "stdout_overflow", "stderr_overflow"])
def test_real_disposable_child_outcomes(tmp_path, kind):
    values = {**VALUES, "bracket": serving.VALID_BRACKET} if kind == "verify" else VALUES
    script = "printf %s " + shlex.quote(output(values).decode()) + "\n"
    if kind == "partial":
        script = "printf 'serving: private marker\\n'\n"
    elif kind == "failure":
        script += "echo 'private marker' >&2\nexit 1\n"
    elif kind.endswith("overflow"):
        script += "/usr/bin/python3 -I -c 'import sys;sys." + ("stderr" if kind.startswith("stderr") else "stdout") + ".write(\"x\"*70000)'\n"
    result = serving.observe(runtime(tmp_path, script), token=TOKEN if kind == "verify" else None)
    assert result["established"] == (kind in {"known", "verify"})
    assert "private marker" not in json.dumps(result)
    if kind == "verify":
        assert result["verified"] and result["bracket"] == TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_only_owned_child(tmp_path, monkeypatch, cancel):
    created = []
    original = serving.asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        created.append(proc)
        return proc
    monkeypatch.setattr(serving.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(serving, "COLLECTION_TIMEOUT", 0.1)
    root = runtime(tmp_path, "exec /usr/bin/sleep 30\n")
    task = asyncio.create_task(serving._capture(root, None))
    if cancel:
        while not created:
            await asyncio.sleep(0.001)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert len(created) == 1 and created[0].returncode < 0
    assert created[0].pid != os.getpgrp()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_owned_grandchild_cannot_keep_output_pipe_open(tmp_path, monkeypatch, cancel):
    created = []
    original = serving.asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        created.append(proc)
        return proc
    monkeypatch.setattr(serving.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(serving, "COLLECTION_TIMEOUT", 0.3)
    marker = tmp_path / "owned-child.pid"
    root = runtime(tmp_path, "/usr/bin/sleep 30 &\n" + "echo $! > " + shlex.quote(str(marker)) + "\nwait\n")
    task = asyncio.create_task(serving._capture(root, None))
    async with asyncio.timeout(2):
        while not marker.exists():
            await asyncio.sleep(0.001)
    child = int(marker.read_text())
    assert os.getpgid(child) == created[0].pid != os.getpgrp()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert created[0].returncode < 0
    # A killed descendant can await init's reap as a zombie; it cannot hold pipes.
    status = Path(f"/proc/{child}/stat")
    assert not status.exists() or status.read_text().split(") ", 1)[1].startswith("Z ")
