"""Managed authorization belongs inside the admitted physical worker."""

from __future__ import annotations

import fcntl
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

HELPER = Path(__file__).resolve().parents[2] / "scripts/lib/code_intel_cbm_worker.py"
spec = importlib.util.spec_from_file_location("managed_worker_test", HELPER)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    binary = tmp_path / "binary"
    binary.write_bytes(b"accepted inode")
    lock = tmp_path / "lifecycle.lock"
    config = dict(binary=str(binary), main=str(tmp_path), cache=str(tmp_path))
    events = []

    @contextmanager
    def shared_lock(*, shared):
        assert shared
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            events.append("locked")
            try:
                yield stream
            finally:
                events.append("released")

    namespace = {
        "lifecycle_lock": shared_lock,
        "config_path": Path,
        "runtime_config": Mock(return_value=config),
        "verify_cache": Mock(),
        "ready": Mock(),
        "require_enabled": Mock(),
        "check_backend": Mock(return_value="123"),
        "verified_binary": lambda path: path.open("rb"),
        "native_env": lambda cfg: {"CBM_CACHE_DIR": cfg["cache"]},
    }
    monkeypatch.setattr(worker, "load_managed", lambda: namespace, raising=False)
    monkeypatch.setattr(worker, "verify_scope", lambda env: 8 * 1024**3)
    marker = tmp_path / "refusal"
    monkeypatch.setenv("CODE_INTEL_CHILD_REFUSAL_MARKER", str(marker))
    args = SimpleNamespace(repo_path=str(tmp_path), mode="full", persistence="true")
    return namespace, config, events, lock, marker, args


def invoke(args):
    return worker.main(
        [
            "--managed-config",
            str(Path(args.repo_path) / "settings.json"),
            "--repo-path",
            args.repo_path,
            "--mode",
            args.mode,
            "--persistence",
            args.persistence,
        ]
    )


def test_physical_spawn_holds_shared_lock_but_wait_does_not(managed, monkeypatch):
    namespace, config, events, lock, marker, args = managed

    def popen(argv, **kwargs):
        assert events == ["locked"]
        with lock.open("a") as stream, pytest.raises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert kwargs["cwd"] == config["main"]
        assert kwargs["env"] == {"CBM_CACHE_DIR": config["cache"]}
        assert argv[argv.index("--index-worker-memory-budget-bytes") + 1] == str(6 * 1024**3)
        response = Path(argv[argv.index("--response-out") + 1])
        response.write_text(json.dumps({"isError": False, "content": []}))

        def wait():
            assert events == ["locked", "released"]
            with lock.open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return 0

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    assert invoke(args) == 0
    namespace["require_enabled"].assert_called_once_with(config)
    assert not marker.exists()
    assert not list(Path(args.repo_path).glob("genesis-worker-*"))


@pytest.mark.parametrize(
    "fault", ["runtime_config", "verify_cache", "ready", "require_enabled", "check_backend"]
)
def test_late_authority_refusals_are_uncharged_before_spawn(managed, monkeypatch, fault):
    namespace, config, events, lock, marker, args = managed
    namespace[fault] = Mock(side_effect=ValueError("revoked"))
    monkeypatch.setattr(worker.subprocess, "Popen", Mock(side_effect=AssertionError("spawn")))
    assert invoke(args) == 111
    assert marker.read_text() == "refused\n"


def test_other_repository_refuses_before_spawn(managed, monkeypatch):
    namespace, config, events, lock, marker, args = managed
    other = Path(args.repo_path) / "other"
    other.mkdir()
    args.repo_path = str(other)
    monkeypatch.setattr(worker.subprocess, "Popen", Mock(side_effect=AssertionError("spawn")))
    assert invoke(args) == 111
    assert marker.read_text() == "refused\n"


@pytest.mark.parametrize("cap", [4 * 1024**3, 9 * 1024**3])
def test_noncanonical_job_cap_refuses(managed, monkeypatch, cap):
    namespace, config, events, lock, marker, args = managed
    monkeypatch.setattr(worker, "verify_scope", lambda env: cap)
    monkeypatch.setattr(worker.subprocess, "Popen", Mock(side_effect=AssertionError("spawn")))
    assert invoke(args) == 111
    assert marker.read_text() == "refused\n"


def test_spawn_error_is_charged_after_authorization(managed, monkeypatch):
    namespace, config, events, lock, marker, args = managed
    monkeypatch.setattr(worker.subprocess, "Popen", Mock(side_effect=OSError("exec failed")))
    assert invoke(args) == 111
    assert not marker.exists()


def test_busy_lifecycle_refuses_before_configuration_read(managed, monkeypatch):
    namespace, config, events, lock, marker, args = managed
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert invoke(args) == 111
    namespace["runtime_config"].assert_not_called()
    assert marker.read_text() == "refused\n"
