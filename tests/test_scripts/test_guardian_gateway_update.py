"""Tests for the guardian-gateway.sh ``update`` op CLAUDE.md regeneration.

The Guardian's diagnostic CC loads the install-dir ``CLAUDE.md`` as project
context, so the ``update`` op must regenerate it from ``config/guardian-claude.md``
on every pull — never leave the repo's container-facing root ``CLAUDE.md`` in
place. Two regressions are guarded here:

* **skip-worktree must not wedge the pull.** If ``CLAUDE.md`` is marked
  ``--skip-worktree`` (older installs did this), ``git pull --ff-only`` aborts
  the moment upstream touches the tracked ``CLAUDE.md`` ("local changes would be
  overwritten"), silently stalling every Guardian update. The op must clear the
  bit before pulling so legacy installs self-heal.
* **No spurious network block.** The regenerated file must equal
  ``config/guardian-claude.md`` exactly — shared host/container facts live in the
  user-level ``~/.claude/CLAUDE.md`` (D16), not duplicated (and half-empty) here.

These run the REAL ``scripts/guardian-gateway.sh update`` against a throwaway git
clone with ``systemctl``/``sudo`` stubbed so the systemd/sysctl side effects
no-op. Real ``git`` is the thing under test.
"""

import io
import os
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"

# Sentinels: the repo-root CLAUDE.md is container-facing; the regenerated file
# must instead be the Guardian identity from config/guardian-claude.md.
_CONTAINER_V1 = "# Genesis v3 — Project Instructions\nCONTAINER SENTINEL v1\n"
_CONTAINER_V2 = "# Genesis v3 — Project Instructions\nCONTAINER SENTINEL v2 UPSTREAM\n"
_GUARDIAN_MD = "# Genesis Guardian — Immune System\nGUARDIAN IDENTITY SENTINEL\n"
_GUARDIAN_YAML = 'container_name: genesis\ncontainer_ip: ""\n'


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def stub_bin(tmp_path):
    """bin dir with no-op systemctl and a sudo that fails ``-n`` (skips sysctl)."""
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 0\n")
    # `sudo -n true` must fail so the host sysctl/udev block is skipped entirely.
    _make_stub(bind / "sudo", "#!/usr/bin/env bash\nexit 1\n")
    return bind


def _seed_repo(home: Path) -> Path:
    """Build a bare remote + an INSTALL_DIR clone in a diverged/legacy state.

    Mirrors a real host: CLAUDE.md locally regenerated to the Guardian identity
    (diverged from the tracked container file), then upstream advances the
    tracked CLAUDE.md. Returns the install dir.
    """
    remote = home / "remote.git"
    _git("-c", "init.defaultBranch=main", "init", "-q", "--bare", str(remote), cwd=home)

    seed = home / "seed"
    _git("clone", "-q", str(remote), str(seed), cwd=home)
    # Force the unborn branch to 'main' regardless of the host git's default
    # (older git ignores an empty remote's HEAD symref when cloning).
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
    _git("config", "user.email", "t@t.t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "CLAUDE.md").write_text(_CONTAINER_V1)
    (seed / "config").mkdir()
    (seed / "config" / "guardian-claude.md").write_text(_GUARDIAN_MD)
    (seed / "config" / "guardian.yaml").write_text(_GUARDIAN_YAML)
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "init", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)

    install = home / ".local" / "share" / "genesis-guardian"
    install.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", str(remote), str(install), cwd=home)
    _git("config", "user.email", "t@t.t", cwd=install)
    _git("config", "user.name", "t", cwd=install)
    # Prior regen state: CLAUDE.md == Guardian identity, diverged from tracked.
    (install / "CLAUDE.md").write_text(_GUARDIAN_MD)

    # Upstream advances the tracked container CLAUDE.md (the trigger for the bug).
    pusher = home / "pusher"
    _git("clone", "-q", str(remote), str(pusher), cwd=home)
    _git("config", "user.email", "t@t.t", cwd=pusher)
    _git("config", "user.name", "t", cwd=pusher)
    (pusher / "CLAUDE.md").write_text(_CONTAINER_V2)
    _git("commit", "-aqm", "upstream change", cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)
    return install


def _run_update(home: Path, stub_bin: Path):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["SSH_ORIGINAL_COMMAND"] = "update"
    proc = subprocess.run(["bash", str(_GATEWAY)], env=env,
                          capture_output=True, text=True)
    return proc


@pytest.mark.parametrize("skip_worktree", [False, True],
                         ids=["clean", "legacy-skip-worktree"])
def test_update_regenerates_guardian_identity_exactly(skip_worktree, stub_bin, tmp_path):
    """`update` pulls, then regenerates CLAUDE.md == guardian-claude.md exactly.

    Must hold whether or not CLAUDE.md was wedged with --skip-worktree (the
    legacy state that aborts the pull on unfixed code).
    """
    home = tmp_path / "home"
    home.mkdir()
    install = _seed_repo(home)
    if skip_worktree:
        _git("update-index", "--skip-worktree", "CLAUDE.md", cwd=install)

    proc = _run_update(home, stub_bin)

    assert proc.returncode == 0, f"update failed: {proc.stdout}\n{proc.stderr}"
    assert '"ok": true' in proc.stdout, proc.stdout
    # Pull actually advanced HEAD to the upstream commit.
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(install),
                          capture_output=True, text=True).stdout.strip()
    remote_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=str(home / "remote.git"),
        capture_output=True, text=True).stdout.strip()
    assert head == remote_head, "update did not fast-forward to upstream"
    # Regenerated file is the Guardian identity, byte-for-byte (no network block,
    # not the upstream container file).
    got = (install / "CLAUDE.md").read_text()
    assert got == _GUARDIAN_MD, f"CLAUDE.md not regenerated cleanly:\n{got!r}"


def _run_redeploy(home: Path, stub_bin: Path, commit: str, tar_bytes: bytes):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["SSH_ORIGINAL_COMMAND"] = f"redeploy {commit}"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", str(_GATEWAY)], env=env, input=tar_bytes,
                          capture_output=True)


def test_redeploy_regenerates_guardian_identity(stub_bin, tmp_path):
    """`redeploy` extracts the pushed tree, then regenerates CLAUDE.md as the
    Guardian identity — never the container-facing root CLAUDE.md in the tar."""
    home = tmp_path / "home"
    home.mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in (("CLAUDE.md", _CONTAINER_V1),
                              ("config/guardian-claude.md", _GUARDIAN_MD)):
            data = content.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

    proc = _run_redeploy(home, stub_bin, "abc1234", buf.getvalue())

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert b'"ok": true' in proc.stdout, proc.stdout
    install = home / ".local" / "share" / "genesis-guardian"
    got = (install / "CLAUDE.md").read_text()
    assert got == _GUARDIAN_MD, f"redeploy left wrong CLAUDE.md:\n{got!r}"
