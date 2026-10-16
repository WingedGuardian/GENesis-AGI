from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
@pytest.mark.parametrize("timing", ["install", "append"])
def test_nonregular_log_never_blocks_or_changes_target(recorder, tmp_path, kind, timing):
    # Run in a child so the original blocking FIFO defect fails with a deadline.
    probe = r'''
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location("isolated_recorder", sys.argv[1])
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)
root, kind, timing = pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4]
log, target = root / "log", root / "unrelated"
target.write_text("untouched\n")
target.chmod(0o644)
def make_target():
    if kind == "fifo": os.mkfifo(log)
    elif kind == "symlink": log.symlink_to(target)
    else: log.mkdir()
os.environ.pop("GENESIS_MCP_ENV_RECORDER", None)
os.environ["RECORDER_SYNTHETIC_KEY"] = "synthetic-canary-value"
if timing == "install": make_target()
recorder.install("memory", declared=frozenset(), declared_prefixes=(),
                 secret_keys=frozenset({"RECORDER_SYNTHETIC_KEY"}), log_path=log)
if timing == "append": make_target()
assert os.getenv("RECORDER_SYNTHETIC_KEY") == "synthetic-canary-value"
assert os.getenv("RECORDER_SYNTHETIC_KEY") == "synthetic-canary-value"
recorder.uninstall()
assert target.read_text() == "untouched\n"
assert target.stat().st_mode & 0o777 == 0o644
'''
    subprocess.run(
        [sys.executable, "-c", probe, recorder.__file__, str(tmp_path), kind, timing],
        check=True, capture_output=True, text=True, timeout=5,
    )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch):
    from genesis.observability import mcp_env_reads

    monkeypatch.delenv("GENESIS_MCP_ENV_RECORDER", raising=False)
    mcp_env_reads.uninstall()
    try:
        yield mcp_env_reads
    finally:
        mcp_env_reads.uninstall()


def _install(
    recorder,
    tmp_path: Path,
    *,
    secret_keys: frozenset[str],
    declared: frozenset[str] = frozenset(),
    declared_prefixes: tuple[str, ...] = (),
) -> Path:
    log_path = tmp_path / "mcp_env_reads.jsonl"
    recorder.install(
        "memory",
        declared=declared,
        declared_prefixes=declared_prefixes,
        secret_keys=secret_keys,
        log_path=log_path,
    )
    return log_path


def _read_with_get(key: str) -> str | None:
    return os.environ.get(key)


def test_preload_reads_a_bounded_snapshot_without_splitting_unicode(recorder, tmp_path, monkeypatch):
    log = tmp_path / "log"
    entry = {"server": "memory", "key": "KEY\u0085NAME", "caller": "module:caller"}
    first = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
    monkeypatch.setattr(recorder, "_MAX_LOG_BYTES", len(first) + 10)
    log.write_bytes(first + b"x" * 100 + b'\n{"server":"late","key":"K","caller":"C"}\n')
    assert recorder._load_seen_keys(log) == {("memory", "KEY\u0085NAME", "module:caller")}


def test_permissions_follow_open_descriptor_when_path_is_replaced(recorder, tmp_path, monkeypatch):
    key = "MCP_ENV_READ_DESCRIPTOR_RACE"
    monkeypatch.setenv(key, "synthetic-value")
    log = _install(recorder, tmp_path, secret_keys=frozenset({key}))
    log.write_text("")
    log.chmod(0o644)
    opened = tmp_path / "opened-log"
    original_open = os.open

    def replace_after_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if Path(path) == log and flags & os.O_WRONLY:
            log.rename(opened)
            log.write_text("replacement remains untouched\n")
            log.chmod(0o644)
        return fd

    monkeypatch.setattr(os, "open", replace_after_open)
    assert _read_with_get(key) == "synthetic-value"
    assert log.read_text() == "replacement remains untouched\n"
    assert log.stat().st_mode & 0o777 == 0o644
    assert opened.stat().st_mode & 0o777 == 0o600
    assert json.loads(opened.read_text())["key"] == key


def _read_with_index(key: str) -> str:
    return os.environ[key]


def _copy_environment() -> dict[str, str]:
    return dict(os.environ)


@pytest.mark.parametrize("access", ["getenv", "get", "contains", "index"])
def test_undeclared_secret_reads_are_logged_once_per_supported_accessor(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    key = f"MCP_ENV_READ_TEST_SECRET_{access.upper()}"
    value = "sentinel-secret-value"
    monkeypatch.setenv(key, value)
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({key}))

    for _ in range(3):
        if access == "getenv":
            assert os.getenv(key) == value
        elif access == "get":
            assert os.environ.get(key) == value
        elif access == "contains":
            assert key in os.environ
        else:
            assert os.environ[key] == value

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [record["key"] for record in records] == [key]


def test_secret_reads_are_deduplicated_per_caller(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_CALLER_DEDUPE"
    value = "caller-dedupe-secret"
    monkeypatch.setenv(key, value)
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({key}))

    assert _read_with_get(key) == value
    assert _read_with_get(key) == value
    assert _read_with_index(key) == value

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [record["caller"] for record in records] == [
        f"{__name__}:_read_with_get",
        f"{__name__}:_read_with_index",
    ]
    assert [record["key"] for record in records] == [key, key]


def test_bulk_copy_does_not_suppress_later_direct_read(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_BULK_COPY"
    value = "bulk-copy-secret"
    monkeypatch.setenv(key, value)
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({key}))

    assert _copy_environment()[key] == value
    assert _read_with_get(key) == value

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [record["caller"] for record in records] == [
        f"{__name__}:_copy_environment",
        f"{__name__}:_read_with_get",
    ]
    assert [record["key"] for record in records] == [key, key]


def test_declared_prefix_and_non_secret_reads_are_not_logged(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared_key = "MCP_ENV_READ_DECLARED"
    prefixed_key = "MCP_ENV_READ_PREFIX_DYNAMIC"
    non_secret_key = "MCP_ENV_READ_NOT_SECRET"
    for key in (declared_key, prefixed_key, non_secret_key):
        monkeypatch.setenv(key, "value")

    log_path = tmp_path / "mcp_env_reads.jsonl"
    recorder.install(
        "health",
        declared=frozenset({declared_key}),
        declared_prefixes=("MCP_ENV_READ_PREFIX_",),
        secret_keys=frozenset({declared_key, prefixed_key}),
        log_path=log_path,
    )

    assert os.getenv(declared_key) == "value"
    assert os.environ.get(prefixed_key) == "value"
    assert os.environ[non_secret_key] == "value"
    assert not log_path.exists()


def test_log_records_key_and_caller_but_never_value(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_LOG"
    value = "sentinel-secret-value"
    monkeypatch.setenv(key, value)
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({key}))

    assert os.getenv(key) == value

    serialized = log_path.read_text()
    record = json.loads(serialized)
    assert set(record) == {"ts", "server", "key", "pid", "caller"}
    assert datetime.fromisoformat(record["ts"])
    assert record["server"] == "memory"
    assert record["key"] == key
    assert record["pid"] == os.getpid()
    assert record["caller"].endswith(":test_log_records_key_and_caller_but_never_value")
    assert value not in serialized


def test_missing_keys_keep_existing_getenv_and_indexing_behavior(recorder, tmp_path: Path) -> None:
    missing_key = "MCP_ENV_READ_TEST_MISSING"
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({missing_key}))

    assert os.getenv(missing_key) is None
    with pytest.raises(KeyError):
        os.environ[missing_key]
    assert len(log_path.read_text().splitlines()) == 1


def test_unwritable_log_path_does_not_break_lookup(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_UNWRITABLE"
    value = "still-available"
    monkeypatch.setenv(key, value)
    log_path = tmp_path / "is-a-directory"
    log_path.mkdir()

    recorder.install(
        "memory",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=log_path,
    )

    assert os.environ.get(key) == value


def test_failed_write_is_retried_by_same_caller(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_WRITE_RETRY"
    value = "retry-secret"
    monkeypatch.setenv(key, value)
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({key}))
    original_open = os.open
    attempts = 0

    def fail_first_open(path, *args, **kwargs):
        nonlocal attempts
        if Path(path) == log_path:
            attempts += 1
            if attempts == 1:
                raise OSError("temporary log failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_first_open)
    assert _read_with_get(key) == value
    assert _read_with_get(key) == value

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert attempts == 2
    assert [record["key"] for record in records] == [key]
    assert records[0]["caller"] == f"{__name__}:_read_with_get"


def test_log_file_permissions_are_restricted(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_key = "MCP_ENV_READ_TEST_PERMISSIONS"
    second_key = "MCP_ENV_READ_TEST_EXISTING_PERMISSIONS"
    monkeypatch.setenv(first_key, "first-secret")
    monkeypatch.setenv(second_key, "second-secret")
    log_dir = tmp_path / "private-logs"
    log_path = log_dir / "mcp_env_reads.jsonl"
    previous_umask = os.umask(0o022)
    try:
        recorder.install(
            "memory",
            declared=frozenset(),
            declared_prefixes=(),
            secret_keys=frozenset({first_key, second_key}),
            log_path=log_path,
        )
        assert _read_with_get(first_key) == "first-secret"
        assert log_path.stat().st_mode & 0o777 == 0o600
        assert log_dir.stat().st_mode & 0o777 == 0o700

        log_path.chmod(0o644)
        assert _read_with_get(second_key) == "second-secret"
        assert log_path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous_umask)


def test_preexisting_triples_are_deduplicated_per_server(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_PREEXISTING"
    value = "preexisting-secret"
    monkeypatch.setenv(key, value)
    log_path = tmp_path / "mcp_env_reads.jsonl"
    caller = f"{__name__}:_read_with_get"
    prior_entry = {
        "ts": datetime.now().isoformat(),
        "server": "memory",
        "key": key,
        "pid": os.getpid(),
        "caller": caller,
    }
    log_path.write_text(json.dumps(prior_entry) + "\n", encoding="utf-8")

    recorder.install(
        "memory",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=log_path,
    )
    assert _read_with_get(key) == value
    assert len(log_path.read_text().splitlines()) == 1

    recorder.install(
        "health",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=log_path,
    )
    assert _read_with_get(key) == value
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [(record["server"], record["key"], record["caller"]) for record in records] == [
        ("memory", key, caller),
        ("health", key, caller),
    ]


def test_malformed_preexisting_lines_are_ignored(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_MALFORMED"
    value = "malformed-secret"
    monkeypatch.setenv(key, value)
    log_path = tmp_path / "mcp_env_reads.jsonl"
    log_path.write_text("{malformed json}\n", encoding="utf-8")
    recorder.install(
        "memory",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=log_path,
    )

    assert _read_with_get(key) == value
    lines = log_path.read_text().splitlines()
    assert lines[0] == "{malformed json}"
    assert json.loads(lines[1])["key"] == key


def test_log_size_cap_keeps_lookups_working(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_LOG_CAP"
    value = "cap-secret"
    monkeypatch.setenv(key, value)
    log_path = tmp_path / "mcp_env_reads.jsonl"
    log_path.write_bytes(b"x" * recorder._MAX_LOG_BYTES)
    recorder.install(
        "memory",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=log_path,
    )

    assert _read_with_get(key) == value
    assert log_path.stat().st_size == recorder._MAX_LOG_BYTES


def test_reinstall_resets_seen_state_for_new_server_and_log_path(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_REINSTALL"
    value = "reinstall-secret"
    monkeypatch.setenv(key, value)
    first_path = tmp_path / "first" / "mcp_env_reads.jsonl"
    second_path = tmp_path / "second" / "mcp_env_reads.jsonl"
    third_path = tmp_path / "third" / "mcp_env_reads.jsonl"

    recorder.install(
        "memory",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=first_path,
    )
    assert _read_with_get(key) == value

    recorder.install(
        "memory",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=second_path,
    )
    assert _read_with_get(key) == value

    recorder.install(
        "health",
        declared=frozenset(),
        declared_prefixes=(),
        secret_keys=frozenset({key}),
        log_path=third_path,
    )
    assert _read_with_get(key) == value

    expected_caller = f"{__name__}:_read_with_get"
    first = json.loads(first_path.read_text().splitlines()[0])
    second = json.loads(second_path.read_text().splitlines()[0])
    third = json.loads(third_path.read_text().splitlines()[0])
    assert [
        (record["server"], record["key"], record["caller"]) for record in (first, second, third)
    ] == [
        ("memory", key, expected_caller),
        ("memory", key, expected_caller),
        ("health", key, expected_caller),
    ]


def test_environment_switch_disables_recording(
    recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "MCP_ENV_READ_TEST_DISABLED"
    monkeypatch.setenv(key, "value")
    monkeypatch.setenv("GENESIS_MCP_ENV_RECORDER", "0")
    original_class = type(os.environ)
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({key}))

    assert os.getenv(key) == "value"
    assert type(os.environ) is original_class
    assert not log_path.exists()


def test_uninstall_restores_original_environment_class(recorder, tmp_path: Path) -> None:
    original_class = type(os.environ)
    _install(recorder, tmp_path, secret_keys=frozenset())

    assert type(os.environ) is not original_class
    recorder.uninstall()
    assert type(os.environ) is original_class


def test_recording_guard_prevents_reentrant_duplicate_reads(
    recorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "MCP_ENV_READ_TEST_REENTRANT"
    value = "reentrant-value"
    monkeypatch.setenv(key, value)
    log_path = _install(recorder, tmp_path, secret_keys=frozenset({key}))
    original_fdopen = os.fdopen

    def reentrant_fdopen(fd, *args, **kwargs):
        assert os.getenv(key) == value
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", reentrant_fdopen)
    assert os.getenv(key) == value
    assert len(log_path.read_text().splitlines()) == 1
