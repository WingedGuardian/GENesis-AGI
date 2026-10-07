"""Coordinator preference and canonical lock failures are admission boundaries."""

from __future__ import annotations

import fcntl
import hashlib
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts import test_code_intel_index as index
from tests.test_scripts.test_code_intel_cbm_worker import _args, _fake_worker, worker


@pytest.mark.parametrize("adjustment", ["inherited", 500, 999, 1000])
def test_supervisor_preserves_score_or_refuses_maximum(tmp_path, adjustment):
    output = tmp_path / "score"
    inherited = int(Path("/proc/self/oom_score_adj").read_text())
    expected = inherited if adjustment == "inherited" else max(inherited, adjustment)
    command = ["/bin/bash", str(index._ENTRYPOINT), "--exec-managed-supervisor",
               "/bin/bash", "-c", 'cat /proc/self/oom_score_adj > "$1"', "probe", str(output)]
    result = subprocess.run(
        command, capture_output=True, text=True,
        preexec_fn=lambda: Path("/proc/self/oom_score_adj").write_text(str(expected)),
    )
    if expected == 1000:
        assert result.returncode == 125
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text().strip() == str(expected)


@pytest.mark.parametrize("raw", ["-1000", "-1", "0", "500", "999", "1000", "1001", "-1001", "invalid", None])
def test_adapter_score_proof_preserves_admission_refusal(tmp_path, monkeypatch, raw):
    marker = tmp_path / "marker"
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/oom_score_adj":
            if raw is None:
                raise OSError("unreadable")
            return raw
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(worker.runpy, "run_path", lambda path: {
        "number": lambda value, label: int(value), "assess": lambda *a, **kw: None,
    })
    env = dict(CODE_INTEL_CHILD_CAP_BYTES=str(worker.JOB_CAP),
               CODE_INTEL_CHILD_RESERVE_BYTES="1", CODE_INTEL_CHILD_REFUSAL_MARKER=str(marker))
    if raw in ("-1000", "-1", "0", "500", "999"):
        assert worker.verify_scope(env) == worker.JOB_CAP
        assert not marker.exists()
    else:
        with pytest.raises((ValueError, OSError)):
            worker.verify_scope(env)
        assert marker.read_text() == "refused\n"


@pytest.mark.parametrize("tool,expected", [("cbm", 75), ("gitnexus", 0), ("both", 5)])
@pytest.mark.parametrize("namespace", ["parent-file", "relative"])
def test_canonical_namespace_refusal_survives_temporary_lock(tmp_path, tool, expected, namespace):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tool.log"
    index._fake_tools(fakebin, log)
    repo = index._make_repo(tmp_path)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    if namespace == "parent-file":
        home = tmp_path / "not-directory"
        home.write_text("preserve")
    else:
        home = Path("relative-lock-namespace")
    result = index._run_entry(
        tmp_path, repo, tool, path=f"{fakebin}:{index._SYSTEM_PATH}",
        env_extra={"GENESIS_HOME": str(home), "TMPDIR": str(fallback)}, cwd=tmp_path,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    text = log.read_text() if log.exists() else ""
    assert "codebase-memory-mcp ARGS" not in text
    assert ("gitnexus ARGS" in text) == (tool != "cbm")
    if namespace == "parent-file":
        assert home.read_text() == "preserve"


def test_held_fallback_does_not_admit_managed_worker(tmp_path):
    fakebin, log = tmp_path / "fakebin", tmp_path / "tool.log"
    index._fake_tools(fakebin, log)
    repo = index._make_repo(tmp_path)
    home = tmp_path / "not-directory"
    home.write_text("preserve")
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    canonical_name = index._lock_file_for(tmp_path, repo).name
    with (fallback / canonical_name).open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = index._run_entry(
            tmp_path, repo, "both", path=f"{fakebin}:{index._SYSTEM_PATH}",
            env_extra={"GENESIS_HOME": str(home), "TMPDIR": str(fallback),
                       "CODE_INTEL_INDEX_LOCK_SKIP_RC": "75"},
        )
    assert result.returncode == 75
    assert not log.exists()


@pytest.mark.parametrize("replace_path", [False, True])
def test_native_child_is_promoted_without_changing_adapter(tmp_path, monkeypatch, replace_path):
    _fake_worker(tmp_path, monkeypatch)
    binary = tmp_path / "binary"
    proof = tmp_path / "native-score"
    binary.write_text(binary.read_text().replace(
        "import json,sys\n", "import json,sys\nfrom pathlib import Path\n"
        f"Path({str(proof)!r}).write_text(Path('/proc/self/oom_score_adj').read_text())\n",
    ))
    monkeypatch.setitem(worker.load_managed()["verified_binary"].__globals__, "BUILD",
                        hashlib.sha256(binary.read_bytes()).hexdigest())
    before = Path("/proc/self/oom_score_adj").read_text()
    actual = worker.subprocess.Popen

    def spawn(argv, **kwargs):
        assert argv[:3] == ["/bin/bash", str(index._ENTRYPOINT), "--exec-indexer-with-oom-adj"]
        assert kwargs["pass_fds"] == (int(argv[3].rsplit("/", 1)[1]),)
        if replace_path:
            replacement = tmp_path / "replacement"
            replacement.write_text("#!/bin/sh\nexit 99\n")
            replacement.chmod(0o700)
            replacement.replace(binary)
        return actual(argv, **kwargs)

    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    assert worker._execute_stock_worker(tmp_path / "settings", _args(tmp_path), worker.JOB_CAP) == 0
    assert proof.read_text().strip() == "1000"
    assert Path("/proc/self/oom_score_adj").read_text() == before


@pytest.mark.parametrize("override", ["999", "invalid"])
def test_supervisor_mode_keeps_unsafe_override_refusal(tmp_path, override):
    result = subprocess.run(
        ["/bin/bash", str(index._ENTRYPOINT), "--exec-managed-supervisor", "/bin/true"],
        env={"CODE_INTEL_INDEX_OOM_SCORE_ADJ": override}, capture_output=True, text=True,
    )
    assert result.returncode == 125


def test_native_promotion_failure_is_charged_without_changing_adapter(tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch)
    monkeypatch.setenv("CODE_INTEL_TEST_FORCE_OOM_ADJ_FAILURE", "1")
    marker = tmp_path / "refusal"
    monkeypatch.setenv("CODE_INTEL_CHILD_REFUSAL_MARKER", str(marker))
    before = Path("/proc/self/oom_score_adj").read_text()
    assert worker._execute_stock_worker(tmp_path / "settings", _args(tmp_path), worker.JOB_CAP) == 111
    assert not marker.exists()  # already authorized spawn, not a pre-index defer
    assert Path("/proc/self/oom_score_adj").read_text() == before
    assert not list(tmp_path.glob("genesis-worker-*"))
