"""Tests for the guardian-gateway.sh `redeploy` verb — F.0 tree-integrity.

These run the REAL gateway script in a sandboxed $HOME (never the live install
tree), with `systemctl` replaced by a PATH shim that records its invocations.
The security-critical surface is: a corrupt/truncated tar (or one built with the
wrong pathspec) must NEVER overwrite a healthy install with a good-looking
`deploy_state.json`. So the sha-mismatch, required-file-gate, rollback, and
legacy-compat paths get direct coverage against the real bash.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "scripts" / "guardian-gateway.sh"

_needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sha256sum") is None,
    reason="requires bash + sha256sum",
)

# The three files the post-extract gate requires to be present and non-empty.
_REQUIRED = (
    "src/genesis/guardian/check.py",
    "scripts/guardian-gateway.sh",
    "pyproject.toml",
)


def _make_tar(members: dict[str, bytes]) -> bytes:
    """Build an uncompressed tar (git-archive-shaped: repo-relative paths)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _good_members() -> dict[str, bytes]:
    """A tree that passes the required-file gate + a sentinel new file."""
    return {
        "src/genesis/guardian/check.py": b"# new check.py\nprint('hi')\n",
        "scripts/guardian-gateway.sh": b"#!/usr/bin/env bash\necho new-gateway\n",
        "pyproject.toml": b"[project]\nname = 'genesis'\n",
        "src/genesis/guardian/__init__.py": b"# new marker\n",
    }


@pytest.fixture
def sandbox(tmp_path):
    """Sandboxed HOME with a pre-existing 'old' guardian install to roll back to."""
    home = tmp_path / "home"
    install = home / ".local" / "share" / "genesis-guardian"
    (install / "src" / "genesis" / "guardian").mkdir(parents=True)
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "state" / "genesis-guardian").mkdir(parents=True)
    (home / "tmp").mkdir(parents=True)

    # An OLD install with a sentinel that must survive any rollback.
    (install / "OLD_SENTINEL").write_text("old-install\n")
    (install / "src" / "genesis" / "guardian" / "check.py").write_text("# OLD check\n")
    (install / "scripts").mkdir()
    (install / "scripts" / "guardian-gateway.sh").write_text("#!/usr/bin/env bash\n# OLD\n")
    (install / "pyproject.toml").write_text("[project]\nname='old'\n")

    state = home / ".local" / "state" / "genesis-guardian"
    (state / "deploy_state.json").write_text(
        json.dumps({"deployed_commit": "0ldc0de", "deployed_at": "2026-01-01T00:00:00+00:00"})
    )

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    calls = tmp_path / "calls.log"
    shim = fakebin / "systemctl"
    shim.write_text(f'#!/bin/sh\necho "systemctl $@" >> "{calls}"\nexit 0\n')
    shim.chmod(0o755)

    return {
        "home": home,
        "install": install,
        "state": state,
        "fakebin": fakebin,
        "calls": calls,
    }


def _run(sandbox: dict, verb: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
    env = {
        "HOME": str(sandbox["home"]),
        "PATH": f"{sandbox['fakebin']}:/usr/bin:/bin",
        "SSH_ORIGINAL_COMMAND": verb,
    }
    return subprocess.run(
        ["bash", str(GATEWAY)],
        env=env,
        input=stdin,
        capture_output=True,
        timeout=60,
    )


def _calls(sandbox: dict) -> str:
    return sandbox["calls"].read_text() if sandbox["calls"].exists() else ""


def _deploy_state(sandbox: dict) -> dict:
    return json.loads((sandbox["state"] / "deploy_state.json").read_text())


def _no_spool_left(sandbox: dict) -> bool:
    return not list(sandbox["state"].glob("redeploy.*.tar"))


@_needs_bash
class TestRedeployVerified:
    def test_verified_deploy_records_tree_sha(self, sandbox):
        tar = _make_tar(_good_members())
        sha = hashlib.sha256(tar).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar)
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["ok"] is True
        assert payload["verified"] is True
        assert payload["commit"] == "abc1234"
        ds = _deploy_state(sandbox)
        assert ds["deployed_commit"] == "abc1234"
        assert ds["tree_sha256"] == sha
        # New tree actually landed; old sentinel gone (clean extract, backup pruned).
        assert (sandbox["install"] / "src" / "genesis" / "guardian" / "__init__.py").exists()
        assert _no_spool_left(sandbox)

    def test_verified_deploy_with_large_archive(self, sandbox):
        # Regression for the pipefail+SIGPIPE membership bug: a real git archive
        # lists ~1900 entries (~86 KB), well over the 64 KB pipe buffer. The old
        # `printf | grep -q` gate short-circuited, printf got SIGPIPE, and
        # pipefail reported the PRESENT required files as missing. Build a tar
        # whose `tar -tf` output far exceeds the pipe buffer, with the required
        # files present, and confirm the verified deploy succeeds.
        members = _good_members()
        # ~3000 long-named dummy entries → listing well over 64 KB.
        for i in range(3000):
            members[f"src/genesis/pkg/really_long_module_name_number_{i:05d}.py"] = b"x\n"
        tar = _make_tar(members)
        sha = hashlib.sha256(tar).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar)
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["ok"] is True
        assert payload["verified"] is True
        assert _deploy_state(sandbox)["deployed_commit"] == "abc1234"
        assert _no_spool_left(sandbox)

    def test_version_advertises_redeploy_verify_and_tree_sha(self, sandbox):
        # Before any verified deploy: capability advertised, tree sha empty.
        res = _run(sandbox, "version")
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["redeploy_verify"] is True
        assert payload["deployed_tree_sha256"] == ""
        assert payload["deployed_commit"] == "0ldc0de"

        # After a verified deploy: version reflects the recorded tree sha.
        tar = _make_tar(_good_members())
        sha = hashlib.sha256(tar).hexdigest()
        assert _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar).returncode == 0
        payload2 = json.loads(_run(sandbox, "version").stdout)
        assert payload2["deployed_tree_sha256"] == sha
        assert payload2["deployed_commit"] == "abc1234"

    def test_legacy_no_sha_form_still_deploys(self, sandbox):
        tar = _make_tar(_good_members())
        res = _run(sandbox, "redeploy abc1234", stdin=tar)
        assert res.returncode == 0, res.stderr
        payload = json.loads(res.stdout)
        assert payload["ok"] is True
        assert payload["verified"] is False
        assert _deploy_state(sandbox)["tree_sha256"] == ""
        assert _no_spool_left(sandbox)


@_needs_bash
class TestRedeployRejects:
    def test_sha_mismatch_leaves_install_untouched(self, sandbox):
        tar = _make_tar(_good_members())
        wrong = "f" * 64
        res = _run(sandbox, f"redeploy abc1234 {wrong}", stdin=tar)
        assert res.returncode == 1
        assert json.loads(res.stderr)["error"] == "archive sha256 mismatch"
        # A bad transfer must not disturb the running guardian AT ALL:
        assert (sandbox["install"] / "OLD_SENTINEL").exists()
        assert _deploy_state(sandbox)["deployed_commit"] == "0ldc0de"
        assert "stop" not in _calls(sandbox)  # timer never touched
        assert _no_spool_left(sandbox)

    def test_missing_required_file_rejected_pre_extract(self, sandbox):
        # An archive missing pyproject.toml (wrong pathspec / partial) — with the
        # CORRECT sha of that archive, so only the membership gate can catch it.
        # The gate runs BEFORE extraction, so the install is never disturbed.
        members = _good_members()
        del members["pyproject.toml"]
        tar = _make_tar(members)
        sha = hashlib.sha256(tar).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar)
        assert res.returncode == 1
        assert json.loads(res.stderr)["error"] == "archive missing required files"
        # Install untouched, deploy_state NOT advanced, timer never stopped.
        assert (sandbox["install"] / "OLD_SENTINEL").exists()
        assert _deploy_state(sandbox)["deployed_commit"] == "0ldc0de"
        assert "stop" not in _calls(sandbox)
        assert _no_spool_left(sandbox)

    def test_invalid_sha_format_rejected(self, sandbox):
        res = _run(sandbox, "redeploy abc1234 nothex", stdin=b"whatever")
        assert res.returncode == 1
        assert json.loads(res.stderr)["error"] == "invalid archive sha256"
        assert "stop" not in _calls(sandbox)

    def test_invalid_commit_hash_rejected(self, sandbox):
        res = _run(sandbox, "redeploy ZZZ " + "a" * 64, stdin=b"whatever")
        assert res.returncode == 1
        assert json.loads(res.stderr)["error"] == "invalid commit hash"
        assert "stop" not in _calls(sandbox)

    def test_timer_restarted_when_post_stop_step_aborts(self, sandbox):
        # Force the deploy_state write to fail (path is a directory) AFTER the
        # timer has been stopped. The EXIT trap must still bring the guardian
        # timer back up — a failed post-stop step must never leave it DOWN.
        (sandbox["state"] / "deploy_state.json").unlink()
        (sandbox["state"] / "deploy_state.json").mkdir()
        tar = _make_tar(_good_members())
        sha = hashlib.sha256(tar).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar)
        assert res.returncode != 0  # aborted on the deploy_state write
        calls = _calls(sandbox)
        assert "stop genesis-guardian.timer" in calls
        assert "start genesis-guardian.timer" in calls  # trap restarted it
        assert _no_spool_left(sandbox)

    def test_broken_local_bin_does_not_abort_deploy(self, sandbox):
        # A failing gateway self-update (cp into a bad .local/bin) is best-effort
        # and guarded — the deploy must still succeed and record deploy_state.
        shutil.rmtree(sandbox["home"] / ".local" / "bin")
        (sandbox["home"] / ".local" / "bin").write_text("not-a-dir\n")
        tar = _make_tar(_good_members())
        sha = hashlib.sha256(tar).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar)
        assert res.returncode == 0, res.stderr
        assert json.loads(res.stdout)["ok"] is True
        assert _deploy_state(sandbox)["deployed_commit"] == "abc1234"

    def test_corrupt_tar_with_matching_sha_rejected(self, sandbox):
        # Random bytes that are NOT a tar, sent with their own correct sha: sha
        # passes, but `tar -tf` lists no members → the membership gate rejects it
        # pre-extraction, so the install is never touched.
        junk = b"\x00\x01\x02not-a-tar" * 100
        sha = hashlib.sha256(junk).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=junk)
        assert res.returncode == 1
        assert json.loads(res.stderr)["error"] == "archive missing required files"
        assert (sandbox["install"] / "OLD_SENTINEL").exists()
        assert _deploy_state(sandbox)["deployed_commit"] == "0ldc0de"
        assert "stop" not in _calls(sandbox)
        assert _no_spool_left(sandbox)


@_needs_bash
class TestRedeployUnitRefresh:
    """The redeploy verb refreshes systemd unit files from the archived repo
    config (copy-if-present) — so push-redeploys stop leaving host units frozen
    at install time (they previously did: only the `update`/git-pull verb copied
    units). An older archive that lacks the units must be a no-op (backward-compat;
    the required-file gate deliberately does not demand them)."""

    def _systemd_unit(self, sandbox: dict, name: str) -> Path:
        return sandbox["home"] / ".config" / "systemd" / "user" / name

    def test_units_present_in_archive_are_copied_and_reloaded(self, sandbox):
        members = _good_members()
        new_unit = b"[Service]\nMemoryMax=80%\nOOMScoreAdjust=0\n"
        members["config/genesis-guardian.service"] = new_unit
        tar = _make_tar(members)
        sha = hashlib.sha256(tar).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar)
        assert res.returncode == 0, res.stderr
        assert json.loads(res.stdout)["ok"] is True
        installed = self._systemd_unit(sandbox, "genesis-guardian.service")
        assert installed.exists(), "unit must be copied into the systemd user dir"
        assert installed.read_bytes() == new_unit
        assert "daemon-reload" in _calls(sandbox)

    def test_archive_without_units_leaves_systemd_untouched(self, sandbox):
        tar = _make_tar(_good_members())
        sha = hashlib.sha256(tar).hexdigest()
        res = _run(sandbox, f"redeploy abc1234 {sha}", stdin=tar)
        assert res.returncode == 0, res.stderr
        assert json.loads(res.stdout)["ok"] is True
        assert not self._systemd_unit(sandbox, "genesis-guardian.service").exists()
