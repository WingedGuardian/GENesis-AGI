"""Immutable managed setup: real CLI, publication and native provider staging."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/codebase_managed.py"


@pytest.fixture
def managed(monkeypatch):
    assert SCRIPT.is_file(), "managed configuration entrypoint is missing"
    old_path = sys.path[:]
    try:
        yield SimpleNamespace(**runpy.run_path(str(SCRIPT)))
    finally:
        sys.path[:] = old_path


def config(tmp_path, managed):
    return dict(
        version=2,
        main=str(tmp_path / "repo"),
        binary=str(tmp_path / "bin"),
        cache=str(tmp_path / "cache"),
        runtime=str(tmp_path / "runtime"),
        sentinel=str(tmp_path / "disabled"),
        build=managed.BUILD,
    )


@pytest.mark.parametrize("kind", ["regular", "link", "dangling", "directory", "fifo"])
def test_publication_never_replaces_existing_artifacts(managed, tmp_path, kind):
    target = tmp_path / "settings"
    foreign = tmp_path / "foreign"
    if kind == "regular":
        target.write_text("old")
    elif kind in ("link", "dangling"):
        if kind == "link":
            foreign.write_text("foreign")
        target.symlink_to(foreign)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    before = target.lstat()
    with pytest.raises(FileExistsError):
        managed.publish_settings(target, {"new": True})
    assert target.lstat().st_ino == before.st_ino
    assert not list(tmp_path.glob("settings.*"))
    if kind == "link":
        assert foreign.read_text() == "foreign"
    elif kind == "dangling":
        assert not foreign.exists()


def test_publication_is_readable_schema2(managed, tmp_path):
    path = tmp_path / "settings"
    value = config(tmp_path, managed)
    managed.publish_settings(path, value)
    assert managed.read_settings(path) == value
    assert path.stat().st_mode & 0o777 == 0o600
    assert "enabled" not in value


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", 1),
        ("version", True),
        ("main", "relative"),
        ("main", None),
        ("main", "/x\ny"),
        ("build", "wrong"),
        ("enabled", True),
    ],
)
def test_schema_and_pin_refuse_execution(managed, tmp_path, key, value):
    data = config(tmp_path, managed)
    data[key] = value
    path = tmp_path / "settings"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        managed.read_settings(path)


@pytest.mark.parametrize("kind", ["link", "dangling", "directory", "fifo"])
def test_settings_nonregular_refuse_without_blocking(managed, tmp_path, kind):
    path = tmp_path / "settings"
    foreign = tmp_path / "foreign"
    if kind in ("link", "dangling"):
        if kind == "link":
            foreign.write_text(json.dumps(config(tmp_path, managed)))
        path.symlink_to(foreign)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        managed.read_settings(path)


def test_stale_pin_remains_diagnosable(managed, tmp_path):
    data = config(tmp_path, managed)
    data["build"] = "old"
    path = tmp_path / "settings"
    path.write_text(json.dumps(data))
    assert managed.read_settings(path, require_build=False) == data


def test_verified_executable_is_same_inode_and_foreign_path_survives(
    managed, tmp_path, monkeypatch
):
    binary = tmp_path / "bin"
    binary.write_bytes(b"accepted")
    binary.chmod(0o500)
    monkeypatch.setattr(
        managed.verified_binary.__globals__["hashlib"],
        "file_digest",
        lambda stream, algorithm: SimpleNamespace(hexdigest=lambda: managed.BUILD),
    )
    with managed.verified_binary(binary) as stream:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"foreign")
        replacement.replace(binary)
        assert stream.read() == b"accepted"
    assert binary.read_bytes() == b"foreign"


@pytest.mark.parametrize("state", ["present", "dangling", "broken-parent", "absent"])
def test_only_definite_sentinel_absence_clears(managed, tmp_path, state):
    sentinel = tmp_path / "disabled"
    if state == "present":
        sentinel.touch()
    elif state == "dangling":
        sentinel.symlink_to(tmp_path / "gone")
    elif state == "broken-parent":
        (tmp_path / "parent").symlink_to(tmp_path / "gone")
        sentinel = tmp_path / "parent/disabled"
    assert managed.sentinel_armed(str(sentinel)) == (state != "absent")


def test_native_environment_scrubs_provider_overrides(managed, tmp_path, monkeypatch):
    monkeypatch.setenv("CBM_AUTO_INDEX", "true")
    monkeypatch.setenv("CBM_CACHE_DIR", "foreign")
    env = managed.native_env(config(tmp_path, managed))
    assert "CBM_AUTO_INDEX" not in env
    assert env["CBM_CACHE_DIR"] == str(tmp_path / "cache")


def test_status_reports_missing_settings_without_starting_provider(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--config", str(tmp_path / "missing"), "status"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["settings"] is None


def test_status_reports_unresolvable_settings_and_still_queries_manager(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--config", str(loop / "settings"), "status"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["settings_error"] and (value["service"] or value["manager_error"])


@pytest.mark.parametrize("depth", [1500, 6000])
def test_status_nested_json_still_reports_manager(tmp_path, depth):
    path = tmp_path / "nested"
    path.write_text('{"extra":' * depth + "0" + "}" * depth)
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--config", str(path), "status"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["settings_error"] and (value["service"] or value["manager_error"])
    assert "extra" not in (value["settings"] or {})


@pytest.mark.parametrize("kind", ["unexecutable", "wrong-pin", "link", "directory", "fifo"])
def test_binary_invalid_kind_or_build_refuses(managed, tmp_path, kind):
    path = tmp_path / "bin"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "link":
        path.symlink_to(tmp_path / "missing")
    else:
        path.write_bytes(b"unsupported")
        path.chmod(0o500 if kind == "wrong-pin" else 0o400)
    with pytest.raises((OSError, ValueError)):
        managed.verified_binary(path)


def test_publication_failure_keeps_settings_and_removes_only_temporary(
    managed, tmp_path, monkeypatch
):
    target = tmp_path / "settings"
    sync = Mock(side_effect=[None, OSError("directory sync failed")])
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError, match="directory sync"):
        managed.publish_settings(target, config(tmp_path, managed))
    assert target.is_file() and managed.read_settings(target)["version"] == 2
    assert sync.call_count == 2
    assert not list(tmp_path.glob("settings.*"))


@pytest.fixture
def native_root():
    if not os.environ.get("GENESIS_TEST_CBM_PINNED_BINARY"):
        pytest.skip("set GENESIS_TEST_CBM_PINNED_BINARY for native staging acceptance")
    # Native IPC has a Unix socket path ceiling; ordinary pytest basetemps are
    # too deep. All writes remain in one owned temporary fixture below ~/tmp.
    with tempfile.TemporaryDirectory(prefix="cbm-", dir=Path.home() / "tmp") as raw:
        yield Path(raw)


@pytest.mark.parametrize("long_runtime", [False, True])
def test_native_configure_cli_e2e(native_root, long_runtime):
    """Opt-in real release binary; fixture owns every provider write."""
    raw = os.environ.get("GENESIS_TEST_CBM_PINNED_BINARY")
    if not raw:
        pytest.skip("set GENESIS_TEST_CBM_PINNED_BINARY for native staging acceptance")
    source = Path(raw)
    home, repo = native_root / "home", native_root / "repo trailing "
    home.mkdir()
    (repo / "scripts/lib").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts/codebase_managed.py")
    shutil.copy2(
        ROOT / "scripts/lib/code_intel_cbm_worker.py", repo / "scripts/lib/code_intel_cbm_worker.py"
    )
    shutil.copy2(
        ROOT / "scripts/lib/code_intel_cbm_admission.py", repo / "scripts/lib/code_intel_cbm_admission.py"
    )
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    settings = home / ".genesis/config/codebase-managed.json"
    state = home / ("staged" + ("x" * 100 if long_runtime else ""))
    sentinel = home / "disabled"
    sentinel.touch()
    command = [
        sys.executable,
        "-I",
        str(repo / "scripts/codebase_managed.py"),
        "configure",
        "--main",
        str(repo),
        "--binary",
        str(source),
        "--state",
        str(state),
        "--sentinel",
        str(sentinel),
    ]
    env = dict(os.environ, HOME=str(home))
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    if long_runtime:
        assert result.returncode == 1
        assert "coordination could not be created (endpoint)" in result.stderr
        assert "Incomplete staging retained" in result.stderr
        assert state.is_dir() and not settings.exists() and sentinel.exists()
        return
    assert result.returncode == 0, result.stderr
    value = json.loads(settings.read_text())
    assert value["version"] == 2 and "enabled" not in value
    assert value["main"] == str(repo)
    assert hashlib.sha256(Path(value["binary"]).read_bytes()).hexdigest() == value["build"]
    assert json.loads((state / "cache/config.json").read_text())["ui_enabled"] is False
    with sqlite3.connect((state / "cache/_config.db").as_uri() + "?mode=ro", uri=True) as db:
        values = dict(db.execute("SELECT key,value FROM config"))
    assert all(
        values.get(key) == "false" for key in ("auto_index", "auto_watch", "watcher_enabled")
    )
    assert sentinel.exists()
    before = settings.read_bytes()
    repeat = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert repeat.returncode == 1 and settings.read_bytes() == before
    assert not list((home / ".config/systemd/user").glob("*.service"))
