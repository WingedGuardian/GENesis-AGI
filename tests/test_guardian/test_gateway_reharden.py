"""Tests for the guardian-gateway.sh `reharden-key` verb + `version` authkey fields.

These run the REAL gateway script in a sandboxed $HOME (never the live
~/.ssh/authorized_keys), with `systemctl`/`systemd-run` replaced by PATH
shims that record their invocations. The dead-man's-switch and the
authorized_keys rewrite invariants are the security-critical surface here,
so they get direct coverage rather than mock-only tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "scripts" / "guardian-gateway.sh"
INSTALLER = REPO_ROOT / "scripts" / "install_guardian.sh"

_needs_tools = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("bash") is None,
    reason="requires bash + ssh-keygen",
)

# The source address sshd would report for the test connection.
TEST_SRC = "10.0.0.42"
TEST_SSH_CONNECTION = f"{TEST_SRC} 51234 10.0.0.1 22"


def _gen_pubkey(tmp_path: Path, name: str, comment: str) -> str:
    """Generate a real ed25519 keypair; return the public-key line."""
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", comment],
        check=True, capture_output=True,
    )
    return (tmp_path / f"{name}.pub").read_text().strip()


@pytest.fixture
def sandbox(tmp_path):
    """Sandboxed HOME with an authorized_keys containing a PRE-#882 guardian
    line (ForceCommand + forwarding blocks, NO no-pty, NO from=) between two
    unrelated keys — mirroring the observed live host state."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".local" / "bin").mkdir(parents=True)

    other1 = _gen_pubkey(tmp_path, "other1", "user@laptop")
    guardian = _gen_pubkey(tmp_path, "guardian", "genesis-guardian-control")
    other2 = _gen_pubkey(tmp_path, "other2", "admin@desktop")

    old_opts = (
        'command="/home/olduser/.local/bin/guardian-gateway.sh",'
        "no-port-forwarding,no-X11-forwarding,no-agent-forwarding"
    )
    ak = home / ".ssh" / "authorized_keys"
    ak.write_text(f"{other1}\n{old_opts} {guardian}\n{other2}\n")
    ak.chmod(0o600)

    # PATH shims: record calls, succeed.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    calls = tmp_path / "calls.log"
    for tool in ("systemctl", "systemd-run"):
        shim = fakebin / tool
        shim.write_text(f'#!/bin/sh\necho "{tool} $@" >> "{calls}"\nexit 0\n')
        shim.chmod(0o755)

    return {"home": home, "ak": ak, "fakebin": fakebin, "calls": calls,
            "guardian_pub": guardian, "others": (other1, other2)}


def _run(sandbox: dict, verb: str, ssh_connection: str | None = TEST_SSH_CONNECTION,
         ) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(sandbox["home"]),
        "PATH": f"{sandbox['fakebin']}:/usr/bin:/bin",
        "SSH_ORIGINAL_COMMAND": verb,
    }
    if ssh_connection is not None:
        env["SSH_CONNECTION"] = ssh_connection
    return subprocess.run(
        ["bash", str(GATEWAY)], env=env, capture_output=True, text=True, timeout=60,
    )


def _guardian_lines(sandbox: dict) -> list[str]:
    return [line for line in sandbox["ak"].read_text().splitlines()
            if "genesis-guardian-control" in line]


def _calls(sandbox: dict) -> str:
    return sandbox["calls"].read_text() if sandbox["calls"].exists() else ""


@_needs_tools
class TestRehardenKeyVerb:
    def test_hardens_unhardened_line_with_self_proving_from(self, sandbox):
        res = _run(sandbox, "reharden-key")
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["ok"] is True
        assert payload["changed"] is True
        assert payload["has_from"] is True

        lines = _guardian_lines(sandbox)
        assert len(lines) == 1
        line = lines[0]
        assert f'from="{TEST_SRC}"' in line
        assert "no-pty" in line
        assert "no-port-forwarding" in line
        assert "no-agent-forwarding" in line
        # command= is rebuilt against THIS home, never taken from input
        assert f'command="{sandbox["home"]}/.local/bin/guardian-gateway.sh"' in line
        # key material preserved exactly
        assert sandbox["guardian_pub"].split()[1] in line

    def test_other_lines_untouched_and_count_preserved(self, sandbox):
        before = sandbox["ak"].read_text().splitlines()
        res = _run(sandbox, "reharden-key")
        assert res.returncode == 0
        after = sandbox["ak"].read_text().splitlines()
        assert len(after) == len(before) == 3
        other1, other2 = sandbox["others"]
        assert other1 in after
        assert other2 in after

    def test_idempotent_second_call_reports_unchanged(self, sandbox):
        assert _run(sandbox, "reharden-key").returncode == 0
        content_after_first = sandbox["ak"].read_text()
        res2 = _run(sandbox, "reharden-key")
        assert res2.returncode == 0
        payload = json.loads(res2.stdout)
        assert payload["ok"] is True
        assert payload["changed"] is False
        assert sandbox["ak"].read_text() == content_after_first

    def test_arms_dead_mans_switch_before_write(self, sandbox):
        _run(sandbox, "reharden-key")
        calls = _calls(sandbox)
        assert "systemd-run" in calls
        assert "--on-active=120" in calls
        # snapshot exists for the restore to copy back
        assert (sandbox["ak"].parent / "authorized_keys.guardian-bak").exists()

    def test_call_cancels_pending_restore(self, sandbox):
        """Any reharden-key arrival authenticated against the CURRENT file —
        living proof it works — so it disarms a pending restore (the confirm
        path for the container's second call)."""
        _run(sandbox, "reharden-key")
        _run(sandbox, "reharden-key")  # confirm call
        calls = _calls(sandbox)
        assert "stop genesis-authkey-restore.timer" in calls

    def test_empty_ssh_connection_falls_back_to_no_from(self, sandbox):
        res = _run(sandbox, "reharden-key", ssh_connection=None)
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["ok"] is True
        assert payload["has_from"] is False
        line = _guardian_lines(sandbox)[0]
        assert "from=" not in line
        assert "no-pty" in line  # still hardened

    def test_refuses_when_no_guardian_line(self, sandbox):
        content = "\n".join(sandbox["others"]) + "\n"
        sandbox["ak"].write_text(content)
        res = _run(sandbox, "reharden-key")
        assert res.returncode != 0
        assert sandbox["ak"].read_text() == content  # untouched

    def test_refuses_when_multiple_guardian_lines(self, sandbox):
        content = sandbox["ak"].read_text()
        dup = content + f"{sandbox['guardian_pub']}\n"
        sandbox["ak"].write_text(dup)
        res = _run(sandbox, "reharden-key")
        assert res.returncode != 0
        assert sandbox["ak"].read_text() == dup  # untouched

    def test_refuses_to_write_when_switch_cannot_arm(self, sandbox):
        """No dead-man's-switch → no write. A failed systemd-run must abort
        BEFORE authorized_keys is modified."""
        shim = sandbox["fakebin"] / "systemd-run"
        shim.write_text("#!/bin/sh\nexit 1\n")
        shim.chmod(0o755)
        before = sandbox["ak"].read_text()
        res = _run(sandbox, "reharden-key")
        assert res.returncode != 0
        assert sandbox["ak"].read_text() == before


@_needs_tools
class TestVersionAuthkeyFields:
    def test_reports_unhardened_state(self, sandbox):
        res = _run(sandbox, "version")
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["authkey_no_pty"] is False
        assert payload["authkey_has_from"] is False

    def test_reports_hardened_state_with_matching_source(self, sandbox):
        assert _run(sandbox, "reharden-key").returncode == 0
        res = _run(sandbox, "version")
        payload = json.loads(res.stdout)
        assert payload["authkey_no_pty"] is True
        assert payload["authkey_has_from"] is True
        assert payload["authkey_from_matches"] is True
        assert len(payload["authkey_observed_src_hash"]) == 64
        assert len(payload["authkey_opts_hash"]) == 64
        # least disclosure: no raw address in the payload
        assert TEST_SRC not in res.stdout

    def test_from_mismatch_detected_when_source_moves(self, sandbox):
        assert _run(sandbox, "reharden-key").returncode == 0
        moved = "10.0.0.99 51234 10.0.0.1 22"
        res = _run(sandbox, "version", ssh_connection=moved)
        payload = json.loads(res.stdout)
        assert payload["authkey_has_from"] is True
        assert payload["authkey_from_matches"] is False

    def test_glob_from_pattern_matches(self, sandbox):
        """An operator-set subnet glob (sshd fnmatch semantics) must read as
        matching — the reconciler must never fight a manual subnet from=."""
        assert _run(sandbox, "reharden-key").returncode == 0
        sandbox["ak"].write_text(
            sandbox["ak"].read_text().replace(
                f'from="{TEST_SRC}"', 'from="10.0.0.*"',
            ),
        )
        res = _run(sandbox, "version")
        payload = json.loads(res.stdout)
        assert payload["authkey_from_matches"] is True

    def test_empty_src_reports_empty_hash(self, sandbox):
        res = _run(sandbox, "version", ssh_connection=None)
        payload = json.loads(res.stdout)
        assert payload["authkey_observed_src_hash"] == ""


class TestOptionsDivergenceGuardrail:
    """The canonical hardened-options string is deliberately duplicated in
    guardian-gateway.sh (hermetic — must work when the install dir is broken)
    and install_guardian.sh. That duplication is safe ONLY while this test
    forces the two literals to stay byte-identical."""

    def _extract(self, path: Path) -> str:
        import re
        text = path.read_text()
        m = re.search(r'GUARD_BASE_OPTS="([^\n]*)"', text)
        assert m, f"GUARD_BASE_OPTS not found in {path.name}"
        return m.group(1)

    def test_gateway_and_installer_opts_identical(self):
        assert self._extract(GATEWAY) == self._extract(INSTALLER)


def test_version_reports_host_linger_tristate(sandbox):
    """host_linger is always emitted as a JSON true/false/null literal — `null`
    (never a bogus `false`) when loginctl can't be probed. Environment-robust:
    asserts the field is present and well-formed regardless of logind state."""
    res = _run(sandbox, "version")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert "host_linger" in payload
    assert payload["host_linger"] in (True, False, None)
