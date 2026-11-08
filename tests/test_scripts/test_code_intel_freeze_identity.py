"""Real freeze must block the actual entrypoint's harmless fake provider."""

import fcntl
import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_freeze import _disarm
from tests.test_scripts.test_code_intel_index import _SYSTEM_PATH, _fake_tools, _run_entry

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["plain", "trailing\n", "two\n\n", "-leading", "-"])
@pytest.mark.parametrize(
    "spelling",
    [
        "absolute",
        "relative",
        "default",
        "default-relative-script",
        "double-leading-slash-freeze",
        "double-leading-slash-entrypoint",
    ],
)
def test_freeze_and_entrypoint_hold_the_same_physical_repository_lock(tmp_path, name, spelling):
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    stripped = Path(str(repo).rstrip("\n"))
    if stripped != repo:
        stripped.mkdir()
        (stripped / ".git").mkdir()
    freeze = ROOT / "scripts/code_intel_freeze.sh"
    args = [str(repo) if spelling == "absolute" else repo.name]
    if spelling in ("default", "default-relative-script"):
        (repo / "scripts").mkdir()
        freeze = repo / "scripts/code_intel_freeze.sh"
        freeze.write_bytes((ROOT / "scripts/code_intel_freeze.sh").read_bytes())
        args = []
        if spelling == "default-relative-script":
            freeze = freeze.relative_to(tmp_path)
    if spelling == "double-leading-slash-freeze":
        args = ["/" + str(repo)]
    entry_repo = "/" + str(repo) if spelling == "double-leading-slash-entrypoint" else str(repo)
    bindir, log = tmp_path / "bin", tmp_path / "tools.log"
    _fake_tools(bindir, log)
    env = {
        "PATH": str(bindir) + os.pathsep + _SYSTEM_PATH,
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(tmp_path / ".genesis"),
        "CDPATH": str(tmp_path),
    }
    output = tmp_path / "freeze.log"
    with output.open("w") as stream:
        proc = subprocess.Popen(
            ["bash", "--", str(freeze), *args],
            cwd=tmp_path,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while "ARMED" not in output.read_text():
                assert proc.poll() is None, output.read_text()
                assert time.monotonic() < deadline, output.read_text()
                time.sleep(0.01)
            digest = hashlib.sha1(os.fsencode(os.path.realpath(repo))).hexdigest()[:16]
            lock = tmp_path / ".genesis/locks" / f"code-intel-{digest}.lock"
            assert lock.exists(), output.read_text()
            with lock.open("a") as handle, pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = _run_entry(
                tmp_path,
                entry_repo,
                "gitnexus",
                "fast",
                path=env["PATH"],
                cwd=tmp_path,
                env_extra={"CODE_INTEL_INDEX_LOCK_SKIP_RC": "75"},
            )
            assert result.returncode == 75, result.stdout + result.stderr
            assert not log.exists(), log.read_text() if log.exists() else ""
        finally:
            _disarm(proc)


@pytest.mark.parametrize("default", [False, True])
def test_failed_physical_resolution_refuses_before_creating_locks(tmp_path, default):
    repo = tmp_path / "repo"
    repo.mkdir()
    freeze = ROOT / "scripts/code_intel_freeze.sh"
    args = [str(repo)]
    if default:
        (repo / "scripts").mkdir()
        freeze = repo / "scripts/code_intel_freeze.sh"
        freeze.write_bytes((ROOT / "scripts/code_intel_freeze.sh").read_bytes())
        args = []
    # Fail only this owned child's directory-resolution primitive. No host
    # permission changes or privileged environment are needed for this branch.
    startup = tmp_path / "failed-cd.sh"
    startup.write_text("cd() { return 1; }\n")
    state = tmp_path / ".genesis"
    env = {
        "PATH": _SYSTEM_PATH,
        "HOME": str(tmp_path),
        "GENESIS_HOME": str(state),
        "BASH_ENV": str(startup),
    }
    output = tmp_path / "freeze.log"
    with output.open("w") as stream:
        proc = subprocess.Popen(
            ["bash", str(freeze), *args],
            cwd=tmp_path,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while proc.poll() is None and "ARMED" not in output.read_text():
                assert time.monotonic() < deadline, output.read_text()
                time.sleep(0.01)
            assert proc.poll() == 1, output.read_text()
            assert not state.exists()
        finally:
            _disarm(proc)
