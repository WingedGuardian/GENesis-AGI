"""Tests for the guardian-gateway.sh provisioning verbs.

Runs the REAL gateway script in a sandboxed $HOME with a stub venv "python"
that echoes its argv as JSON — so we verify BOTH the arg-validation gate
(injection / range / prefix rejection, before anything runs) AND that a valid
call extracts and forwards exactly the right <disk>/<GiB>/<MiB> to the guardian
CLI. Security-critical surface: SSH_ORIGINAL_COMMAND is untrusted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "scripts" / "guardian-gateway.sh"

_needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None, reason="requires bash",
)

_STUB_PYTHON = """#!/bin/sh
# Fake venv python: echo all argv as a JSON array, ignore -m semantics.
printf '{"ok": true, "argv": ['
first=1
for a in "$@"; do
  [ "$first" -eq 1 ] || printf ', '
  printf '"%s"' "$a"
  first=0
done
printf ']}\\n'
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    install = home / ".local" / "share" / "genesis-guardian"
    venv_bin = install / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (install / "config").mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text(_STUB_PYTHON)
    py.chmod(0o755)
    (install / "config" / "guardian.yaml").write_text("provisioning:\n  enabled: false\n")
    (install / "secrets.env").write_text("")
    return {"home": home}


def _run(sandbox: dict, verb: str) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(sandbox["home"]),
        "PATH": "/usr/bin:/bin",
        "SSH_ORIGINAL_COMMAND": verb,
    }
    return subprocess.run(
        ["bash", str(GATEWAY)], env=env, capture_output=True, text=True, timeout=60,
    )


@_needs_bash
class TestProvisionStatus:
    def test_status_runs_the_cli(self, sandbox):
        r = _run(sandbox, "provision-status")
        assert r.returncode == 0
        argv = json.loads(r.stdout)["argv"]
        assert "--provision-status" in argv


@_needs_bash
class TestGrowDiskValidation:
    def test_valid_forwards_disk_and_gib(self, sandbox):
        r = _run(sandbox, "provision-grow-disk scsi1 32")
        assert r.returncode == 0, r.stderr
        argv = json.loads(r.stdout)["argv"]
        assert "--provision-grow-disk" in argv and "scsi1" in argv and "32" in argv

    def test_bad_prefix_rejected(self, sandbox):
        r = _run(sandbox, "provision-grow-disk sda1 32")
        assert r.returncode == 1
        assert "invalid args" in r.stderr

    def test_out_of_range_gib_rejected(self, sandbox):
        r = _run(sandbox, "provision-grow-disk scsi1 1000")
        assert r.returncode == 1
        assert "invalid args" in r.stderr

    def test_semicolon_injection_rejected(self, sandbox):
        r = _run(sandbox, "provision-grow-disk scsi1 32; echo pwned")
        assert r.returncode == 1
        assert "pwned" not in r.stdout and "pwned" not in r.stderr

    def test_newline_injection_rejected(self, sandbox):
        r = _run(sandbox, "provision-grow-disk scsi1 32\necho INJECT")
        assert r.returncode == 1
        assert "INJECT" not in r.stdout


@_needs_bash
class TestGrowMemoryValidation:
    def test_valid_forwards_mib(self, sandbox):
        r = _run(sandbox, "provision-grow-memory 24576")
        assert r.returncode == 0, r.stderr
        argv = json.loads(r.stdout)["argv"]
        assert "--provision-grow-memory" in argv and "24576" in argv

    def test_too_small_rejected(self, sandbox):
        r = _run(sandbox, "provision-grow-memory 99")
        assert r.returncode == 1
        assert "invalid MiB" in r.stderr


@_needs_bash
class TestStorageExpand:
    def test_emits_valid_json(self, sandbox):
        """Either the stub runs (argv has --storage-expand) or the sudo guard
        fires (passwordless sudo unavailable) — both are well-formed JSON for
        the storage-expand action, never a bare set -e abort."""
        r = _run(sandbox, "storage-expand")
        payload = json.loads(r.stdout or r.stderr)
        if "argv" in payload:
            assert "--storage-expand" in payload["argv"]
        else:
            assert payload["action"] == "storage-expand" and payload["ok"] is False


@_needs_bash
def test_unknown_verb_denied(sandbox):
    r = _run(sandbox, "provision-nuke-everything")
    assert r.returncode == 1
    assert "denied" in r.stderr
