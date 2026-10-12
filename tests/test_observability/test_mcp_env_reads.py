from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest


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
    original_open = Path.open

    def reentrant_open(path: Path, *args, **kwargs):
        if path == log_path:
            assert os.getenv(key) == value
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reentrant_open)
    assert os.getenv(key) == value
    assert len(log_path.read_text().splitlines()) == 1
