"""Closed status projection and owned-child failures, using disposable scripts."""

import asyncio
import importlib.util
import json
import os
import shlex
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_validator_serving as serving  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "closed_serving_request", SCRIPTS / "codex_validator_request.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
TOKEN = "b1-" + "a" * 24
VALUES = {
    "serving": "1" * 40,
    "head": "2" * 40,
    "mainpid": "321",
    "invocation": "3" * 32,
    "runtime-edits": "none",
    "runtime-overrides": "none",
    "bracket": TOKEN,
}


def output(values=None):
    return (
        "\n".join(f"{k}: {v}" for k, v in (VALUES if values is None else values).items()) + "\n"
    ).encode()


def runtime(tmp_path, script):
    root = tmp_path / "runtime's space"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/deploy_code_only.sh").write_text(script)
    return root


async def wait_ready(task, ready):
    async with asyncio.timeout(2):
        while not ready():
            if task.done():
                await task  # Surface launch failure instead of waiting on a dead task.
                pytest.fail("Collection ended before fixture readiness")
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("field", sorted(VALUES))
def test_every_field_is_required_and_duplicates_refuse(field):
    with pytest.raises(ValueError):
        serving.project(output({k: v for k, v in VALUES.items() if k != field}))
    for extra in (f"{field}: {VALUES[field]}\n", f"{field}:{VALUES[field]}\n"):
        with pytest.raises(ValueError):
            serving.project(output() + extra.encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("serving", "unknown (private path)"),
        ("head", "F" * 40),
        ("mainpid", "0"),
        ("mainpid", "-1"),
        ("mainpid", "01"),
        ("invocation", "unknown"),
        ("runtime-edits", "1 paths: private path"),
        ("runtime-overrides", "unreadable"),
        ("runtime-overrides", "0 files, " + "a" * 16),
        ("bracket", "unknown (private marker)"),
    ],
)
def test_unknown_and_malformed_identity_refuse(field, value):
    with pytest.raises(ValueError):
        serving.project(output({**VALUES, field: value}))


def test_only_projected_fields_escape_and_docs_head_may_differ():
    result = serving.project(output() + b"  PENDING: private/path\nexplanation: secret marker\n")
    assert result == {**VALUES, "mainpid": 321, "established": True}
    assert result["head"] != result["serving"]
    assert serving.project(output({**VALUES, "runtime-overrides": "1 files, " + "a" * 16}))[
        "established"
    ]
    with pytest.raises(UnicodeDecodeError):
        serving.project(output() + b"\xff")


def test_verify_uses_the_real_sentence_and_requested_token():
    result = serving.project(output({**VALUES, "bracket": serving.VALID_BRACKET}), token=TOKEN)
    assert result["bracket"] == TOKEN and result["verified"] and result["established"]
    for value in (TOKEN, "INVALID — private state", serving.VALID_BRACKET + " extra"):
        with pytest.raises(ValueError):
            serving.project(output({**VALUES, "bracket": value}), token=TOKEN)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": True, "operation": "serving_status"},
        {"version": 1.0, "operation": "serving_status"},
        {"version": 1, "operation": []},
        {"version": 1, "operation": "serving_status", "token": TOKEN},
        {"version": 1, "operation": "serving_verify"},
        *[
            {"version": 1, "operation": "serving_verify", "token": t}
            for t in (None, False, [], "b1-", TOKEN + "\n")
        ],
        {"version": 1, "operation": "shell"},
    ],
)
def test_closed_schema_refuses_before_child(payload, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid schema entered serving observation")

    monkeypatch.setattr(serving, "observe", forbidden)
    with pytest.raises(ValueError):
        runner.execute(payload)


@pytest.mark.parametrize("operation", ["serving_status", "serving_verify"])
def test_runner_passes_only_launch_owned_root_and_exact_token(monkeypatch, operation):
    calls = []
    monkeypatch.setattr(
        serving,
        "observe",
        lambda root, **kwargs: calls.append((root, kwargs)) or {"established": False},
    )
    request = {"version": 1, "operation": operation}
    if operation == "serving_verify":
        request["token"] = TOKEN
    assert runner.execute(request) == {"version": 1, "operation": operation, "established": False}
    assert calls == [
        (runner.RUNTIME_ROOT, {"token": TOKEN if operation == "serving_verify" else None})
    ]


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
    for key in (
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
        "GIT_CONFIG_COUNT",
        "PYTHONPATH",
        "GENESIS_DEPLOY_ROOT",
    ):
        monkeypatch.setenv(key, "private marker")
    assert await serving._capture(tmp_path, token) == output()
    expected = ("/bin/bash", str(tmp_path / "scripts/deploy_code_only.sh"), "status")
    assert captured["argv"] == expected + (("--verify", token) if token is not None else ())
    assert captured["env"] == serving.child_environment()
    assert not any(
        k in captured["env"]
        for k in (
            "BASH_ENV",
            "ENV",
            "LD_PRELOAD",
            "GIT_CONFIG_COUNT",
            "PYTHONPATH",
            "GENESIS_DEPLOY_ROOT",
        )
    )
    assert captured["start_new_session"] and captured["stdin"] == asyncio.subprocess.DEVNULL
    assert captured["cwd"] == tmp_path


def status_script(first=None, verified=None, log=None):
    """A disposable deploy_code_only.sh: `status` prints FIRST; `status --verify T`
    prints VERIFIED (the valid sentence by default) and exits 0."""
    first = VALUES if first is None else first
    verified = {**first, "bracket": serving.VALID_BRACKET} if verified is None else verified
    record = f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n' if log else ""
    return (
        record
        + 'if [ "$2" = --verify ]; then printf %s '
        + shlex.quote(output(verified).decode())
        + "; else printf %s "
        + shlex.quote(output(first).decode())
        + "; fi\n"
    )


@pytest.mark.parametrize(
    "kind", ["known", "verify", "partial", "failure", "stdout_overflow", "stderr_overflow"]
)
def test_real_disposable_child_outcomes(tmp_path, kind):
    values = {**VALUES, "bracket": serving.VALID_BRACKET} if kind == "verify" else VALUES
    script = "printf %s " + shlex.quote(output(values).decode()) + "\n"
    if kind == "known":
        script = status_script()
    if kind == "partial":
        script = "printf 'serving: private marker\\n'\n"
    elif kind == "failure":
        script += "echo 'private marker' >&2\nexit 1\n"
    elif kind.endswith("overflow"):
        script += (
            "/usr/bin/python3 -I -c 'import sys;sys."
            + ("stderr" if kind.startswith("stderr") else "stdout")
            + '.write("x"*70000)\'\n'
        )
    result = serving.observe(runtime(tmp_path, script), token=TOKEN if kind == "verify" else None)
    assert result["established"] == (kind in {"known", "verify"})
    assert "private marker" not in json.dumps(result)
    if kind == "verify":
        assert result["verified"] and result["bracket"] == TOKEN


@pytest.mark.parametrize("token", [None, TOKEN])
def test_observation_uses_one_self_consistent_producer_read(tmp_path, token):
    """The producer now prints and hashes the same captured inputs (#3398)."""
    log = tmp_path / "calls.log"
    result = serving.observe(runtime(tmp_path, status_script(log=log)), token=token)
    assert result == {
        **VALUES,
        "mainpid": 321,
        "established": True,
        **({"verified": True} if token else {}),
    }
    assert log.read_text().splitlines() == [f"status --verify {TOKEN}" if token else "status"]


@pytest.mark.asyncio
async def test_cancellation_during_creation_kills_the_new_group(tmp_path, monkeypatch):
    """The child exists in its own session before its handle reaches _capture."""
    created = []
    original = serving.asyncio.create_subprocess_exec

    async def slow_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        created.append(proc)
        await asyncio.sleep(0.2)  # cancellation lands here, before the handle returns
        return proc

    monkeypatch.setattr(serving.asyncio, "create_subprocess_exec", slow_spawn)
    root = runtime(tmp_path, "exec /usr/bin/sleep 30\n")
    task = asyncio.create_task(serving._capture(root, None))
    await wait_ready(task, lambda: created)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created[0].returncode is not None and created[0].returncode < 0
    assert created[0].pid != os.getpgrp()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_creation_still_kills_the_new_group(
    tmp_path, monkeypatch
):
    """A second cancel while recovery waits for the handle must not abandon it."""
    created = []
    original = serving.asyncio.create_subprocess_exec

    async def slow_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        created.append(proc)
        await asyncio.sleep(0.2)  # both cancellations land here, before the handle returns
        return proc

    monkeypatch.setattr(serving.asyncio, "create_subprocess_exec", slow_spawn)
    root = runtime(tmp_path, "exec /usr/bin/sleep 30\n")
    task = asyncio.create_task(serving._capture(root, None))
    await wait_ready(task, lambda: created)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created[0].returncode is not None and created[0].returncode < 0


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
        await wait_ready(task, lambda: created)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert len(created) == 1 and created[0].returncode < 0
    assert created[0].pid != os.getpgrp()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("leader_exits", [False, True])
async def test_owned_grandchild_cannot_keep_output_pipe_open(
    tmp_path, monkeypatch, cancel, leader_exits
):
    created = []
    original = serving.asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(serving.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(serving, "COLLECTION_TIMEOUT", 2)
    marker = tmp_path / "owned-child.pid"
    root = runtime(
        tmp_path,
        "/usr/bin/sleep 30 &\n"
        + "echo $! > "
        + shlex.quote(str(marker))
        + ("\nexit 0\n" if leader_exits else "\nwait\n"),
    )
    task = asyncio.create_task(serving._capture(root, None))
    # The file can exist before its pid is written or the handle is returned.
    await wait_ready(
        task,
        lambda: (
            created
            and marker.exists()
            and marker.read_text().strip()
            and (not leader_exits or created[0].returncode is not None)
        ),
    )
    child = int(marker.read_text())
    assert os.getpgid(child) == created[0].pid != os.getpgrp()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert created[0].returncode == 0 if leader_exits else created[0].returncode < 0
    # A killed descendant can await init's reap as a zombie; it cannot hold pipes.
    # Each read is one read: a check-then-read races init's reap, which removes
    # the entry (FileNotFoundError) or empties it mid-read (ESRCH) — both mean it
    # is gone. SIGKILL is delivered when the process next runs, so on a loaded
    # host it can still read R (running, handling the kill) for a moment; a
    # failed kill would leave the sleeper in S. Poll briefly for Z, X or gone.
    deadline = time.monotonic() + 5
    while True:
        try:
            stat = Path(f"/proc/{child}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return
        state = stat.split(") ", 1)[1][:2]
        if state in ("Z ", "X ") or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.02)
    # Z (zombie) or X (dead, mid-reap): either way it holds no pipe.
    assert state in ("Z ", "X ")
