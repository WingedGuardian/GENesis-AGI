"""Tests for the guardian-gateway.sh ``update``/``sync-gateway``/``version`` ops.

The Guardian's diagnostic CC loads the install-dir ``CLAUDE.md`` as project
context, so the ``update`` op must regenerate it from ``config/guardian-claude.md``
on every pull — never leave the repo's container-facing root ``CLAUDE.md`` in
place. The op must also self-update the deployed gateway and report success
reliably. These regressions are guarded here:

* **skip-worktree must not wedge the pull.** If ``CLAUDE.md`` is marked
  ``--skip-worktree`` (older installs did this), ``git pull --ff-only`` aborts
  the moment upstream touches the tracked ``CLAUDE.md`` ("local changes would be
  overwritten"), silently stalling every Guardian update. The op must clear the
  bit before pulling so legacy installs self-heal.
* **No spurious network block.** The regenerated file must equal
  ``config/guardian-claude.md`` exactly — shared host/container facts live in the
  user-level ``~/.claude/CLAUDE.md`` (D16), not duplicated (and half-empty) here.
* **Best-effort host tuning must not abort the update (Bug A).** On a host WITH
  passwordless sudo, an unguarded ``sudo cp``/``sudo tee`` in the sysctl/udev
  block used to abort under ``set -euo pipefail`` *before* the success JSON —
  so ``update`` exited 1, swallowed its result, and (on some orderings) skipped
  the self-update. The deployed gateway then silently froze. The tuning must be
  best-effort: ``update`` self-updates and emits ``{"ok":true}`` regardless.
* **deployed_commit is recorded.** ``update`` writes the new HEAD to
  ``deploy_state.json`` so the watchdog's drift detection works for pull-based
  installs (previously only ``redeploy`` wrote it → drift detection silently
  skipped).
* **sync-gateway / gateway_sha.** A recovery verb re-deploys the install-dir
  gateway without a pull, and ``version`` reports the deployed gateway's sha256
  so a stale gateway is detectable.

These run the REAL ``scripts/guardian-gateway.sh`` against throwaway git clones
with ``systemctl``/``sudo`` stubbed so the systemd/sysctl side effects no-op.
Real ``git`` is the thing under test.
"""

import hashlib
import io
import json
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
# Stand-in for the tracked scripts/guardian-gateway.sh: lets us assert the
# self-update / sync-gateway actually deployed the install-dir copy.
_SEED_GATEWAY = "#!/usr/bin/env bash\n# SEED GATEWAY SENTINEL\necho seed\n"


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


@pytest.fixture
def stub_bin_sudo_passwordless(tmp_path):
    """systemctl no-op; sudo where ``-n true`` SUCCEEDS but every real sudo
    command FAILS — the passwordless-sudo host whose sysctl/udev tuning errors.

    This is the path that hid Bug A: the prior ``stub_bin`` makes ``sudo -n true``
    fail, so the whole tuning block was skipped and never exercised. Here the
    block runs and its commands fail — ``update`` must NOT abort.
    """
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 0\n")
    _make_stub(bind / "sudo",
               '#!/usr/bin/env bash\n[ "$1" = "-n" ] && exit 0\nexit 1\n')
    return bind


def _seed_repo(home: Path) -> Path:
    """Build a bare remote + an INSTALL_DIR clone in a diverged/legacy state.

    Mirrors a real host: CLAUDE.md locally regenerated to the Guardian identity
    (diverged from the tracked container file), then upstream advances the
    tracked CLAUDE.md. The tracked tree includes scripts/guardian-gateway.sh so
    the self-update path is exercised. Returns the install dir.
    """
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)

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
    (seed / "scripts").mkdir()
    (seed / "scripts" / "guardian-gateway.sh").write_text(_SEED_GATEWAY)
    _git("add", "CLAUDE.md", "config/guardian-claude.md", "config/guardian.yaml",
         "scripts/guardian-gateway.sh", cwd=seed)
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


def _run_verb(home: Path, stub_bin: Path, verb: str, stdin_bytes: bytes | None = None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["SSH_ORIGINAL_COMMAND"] = verb
    if stdin_bytes is None:
        return subprocess.run(["bash", str(_GATEWAY)], env=env,
                              capture_output=True, stdin=subprocess.DEVNULL)
    return subprocess.run(["bash", str(_GATEWAY)], env=env,
                          capture_output=True, input=stdin_bytes)


def _run_update(home: Path, stub_bin: Path):
    return _run_verb(home, stub_bin, "update")


def _short_head(install: Path) -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(install),
                          capture_output=True, text=True).stdout.strip()


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
    assert b'"ok": true' in proc.stdout, proc.stdout
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


def test_update_self_updates_and_succeeds_when_sudo_tuning_fails(
        stub_bin_sudo_passwordless, tmp_path):
    """Bug A regression: on a passwordless-sudo host whose sysctl/udev `sudo`
    commands fail, `update` must STILL self-update the deployed gateway and exit
    0 with JSON. The best-effort host tuning must never abort the update."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_repo(home)

    proc = _run_update(home, stub_bin_sudo_passwordless)

    assert proc.returncode == 0, (
        f"update aborted on best-effort sudo tuning failure (Bug A): "
        f"{proc.stdout}\n{proc.stderr}")
    assert b'"ok": true' in proc.stdout, proc.stdout
    deployed = (home / ".local" / "bin" / "guardian-gateway.sh").read_text()
    assert deployed == _SEED_GATEWAY, (
        "self-update did not deploy the install-dir gateway despite a clean pull")


def test_update_writes_deployed_commit(stub_bin, tmp_path):
    """`update` records the new HEAD in deploy_state.json so the watchdog drift
    check (which reads deployed_commit) works for pull-based installs."""
    home = tmp_path / "home"
    home.mkdir()
    install = _seed_repo(home)

    proc = _run_update(home, stub_bin)

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    ds = home / ".local" / "state" / "genesis-guardian" / "deploy_state.json"
    assert ds.exists(), "deploy_state.json not written by update"
    assert json.loads(ds.read_text())["deployed_commit"] == _short_head(install)


def test_update_conflict_preserves_local_config_in_stash(stub_bin, tmp_path):
    """On a `git stash pop` CONFLICT, the operator's local config must be PRESERVED
    in the stash (recoverable), NOT silently `git stash drop`ped.

    The bug: when a local config change (e.g. container_ip) conflicts with an
    upstream change to the same line, `update` used to `git checkout -- .` +
    `git stash drop`, destroying the local config. The fix resets the worktree to a
    clean post-pull state (gateway stays functional) but keeps the stash, and the
    JSON reports a *recoverable* warning.
    """
    home = tmp_path / "home"
    home.mkdir()
    install = _seed_repo(home)

    # Local uncommitted config change in the install dir → becomes the stash.
    (install / "config" / "guardian.yaml").write_text(
        'container_name: genesis\ncontainer_ip: "LOCAL-ONLY-VALUE"\n')

    # Upstream changes the SAME line differently → `git stash pop` will conflict.
    pusher2 = home / "pusher2"
    _git("clone", "-q", str(home / "remote.git"), str(pusher2), cwd=home)
    _git("config", "user.email", "t@t.t", cwd=pusher2)
    _git("config", "user.name", "t", cwd=pusher2)
    (pusher2 / "config" / "guardian.yaml").write_text(
        'container_name: genesis\ncontainer_ip: "UPSTREAM-VALUE"\n')
    _git("commit", "-aqm", "upstream guardian.yaml change", cwd=pusher2)
    _git("push", "-q", "origin", "main", cwd=pusher2)

    proc = _run_update(home, stub_bin)

    # update still succeeds and reports the *recoverable* (not discarded) warning.
    # The ENTIRE stdout must parse as JSON even on this path — a conflicted
    # `git stash pop` is the noisiest case, so this is the strongest JSON-purity guard.
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["ok"] is True, data
    assert "preserved in git stash" in data["warning"], (
        f"conflict warning must say the config is recoverable, not discarded:\n{data}")

    # The stash was NOT dropped — the operator's local config is recoverable.
    stash_list = subprocess.run(["git", "stash", "list"], cwd=str(install),
                                capture_output=True, text=True).stdout
    assert stash_list.strip(), "stash was dropped — local config silently lost (the bug)"
    stash_show = subprocess.run(["git", "stash", "show", "-p"], cwd=str(install),
                                capture_output=True, text=True).stdout
    assert "LOCAL-ONLY-VALUE" in stash_show, (
        f"stash does not contain the operator's local config value:\n{stash_show}")

    # guardian.yaml is reset cleanly to the upstream state — no leftover conflict
    # markers (the gateway worktree stays functional for the next pull).
    gy = (install / "config" / "guardian.yaml").read_text()
    assert "<<<<<<<" not in gy and "UPSTREAM-VALUE" in gy, (
        f"guardian.yaml left in conflict / not reset to upstream:\n{gy!r}")


def test_update_response_is_pure_json(stub_bin, tmp_path):
    """The `update` verb's stdout contract is JSON-ONLY: the container
    (`remote.py`) parses the whole stdout with `json.loads`. A real-change pull
    emits git's diffstat ("Updating.. / Fast-forward / <stats>") to STDOUT; if that
    leaks before the JSON, `json.loads` throws and a SUCCESSFUL update is misread as
    {"ok": false}. Every git command in the verb must suppress stdout so the
    response stays pure JSON.
    """
    home = tmp_path / "home"
    home.mkdir()
    _seed_repo(home)  # upstream advances CLAUDE.md → `git pull` produces a diffstat

    proc = _run_update(home, stub_bin)

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The ENTIRE stdout must be valid JSON — no git diffstat/chatter prefix.
    data = json.loads(proc.stdout)  # raises if polluted → guards the contract
    assert data["ok"] is True, data
    assert data["action"] == "update", data


def test_sync_gateway_redeploys_from_install_dir(stub_bin, tmp_path):
    """`sync-gateway` copies the install-dir gateway to ~/.local/bin WITHOUT a
    pull — the recovery lever when the update self-update path is unavailable."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_repo(home)
    deployed_path = home / ".local" / "bin" / "guardian-gateway.sh"
    deployed_path.write_text("#!/usr/bin/env bash\n# STALE FROZEN GATEWAY\n")

    proc = _run_verb(home, stub_bin, "sync-gateway")

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert b'"ok": true' in proc.stdout, proc.stdout
    assert deployed_path.read_text() == _SEED_GATEWAY, (
        "sync-gateway did not refresh the deployed gateway from the install dir")


def test_version_reports_gateway_sha(stub_bin, tmp_path):
    """`version` reports gateway_sha = sha256 of the deployed gateway, so the
    container can detect a stale/frozen deployed gateway."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_repo(home)
    deployed_path = home / ".local" / "bin" / "guardian-gateway.sh"
    content = "#!/usr/bin/env bash\n# DEPLOYED GATEWAY CONTENT\n"
    deployed_path.write_text(content)

    proc = _run_verb(home, stub_bin, "version")

    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout)
    assert data["gateway_sha"] == hashlib.sha256(content.encode()).hexdigest(), data


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


# ── update-node verb ─────────────────────────────────────────────────────────
# CC only runs on the Node major its pin requires (2.1.198 → node >=22); the host
# runs `claude -p` for Guardian's recovery, so the host Node must be alignable
# in-band. These exercise the verb hermetically — sudo/curl/node are stubbed so
# nothing real is installed and no network is hit.

def _make_node_bin(tmp_path, *, node_version="v22.22.2", sudo_ok=True):
    """PATH dir for the `update-node` verb. Stubs systemctl (no-op), sudo
    (passwordless ok/blocked; install commands are no-op 0 so nothing runs),
    curl (no network), and node (fixed version → asserts verify-by-`node
    --version`, never the apt exit code)."""
    bind = tmp_path / "nodebin"
    bind.mkdir()
    _make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 0\n")
    if sudo_ok:
        # `sudo -n true` passes AND every `sudo -n <cmd>` is a no-op 0, so the
        # "install" trivially succeeds without touching the system.
        _make_stub(bind / "sudo", "#!/usr/bin/env bash\nexit 0\n")
    else:
        _make_stub(bind / "sudo", '#!/usr/bin/env bash\nexit 1\n')
    _make_stub(bind / "curl", "#!/usr/bin/env bash\nexit 0\n")
    _make_stub(bind / "node", f'#!/usr/bin/env bash\necho "{node_version}"\n')
    return bind


@pytest.mark.parametrize("bad", ["2.2", "abc", "22x", "123", ""],
                         ids=["semver", "word", "suffix", "three-digit", "empty"])
def test_update_node_rejects_non_major(bad, tmp_path):
    """The major is interpolated into a privileged NodeSource URL + install, so
    only a bare 1-2 digit major is accepted (mirrors update-cc's semver guard).
    (The empty arg still enters the verb — the f-string keeps a trailing space —
    and is caught by the same invalid-major guard.)"""
    home = tmp_path / "home"
    home.mkdir()
    bind = _make_node_bin(tmp_path)
    proc = _run_verb(home, bind, f"update-node {bad}")
    assert proc.returncode == 1, proc.stdout
    blob = proc.stdout + proc.stderr
    assert b'"ok": false' in blob and b"invalid major" in blob, blob


def test_update_node_rejects_newline_injection(tmp_path):
    """Whole-string bash-regex validation: a major with an embedded newline —
    which the old line-oriented `grep '^…$'` would accept on its first line — is
    rejected before any privileged install runs."""
    home = tmp_path / "home"
    home.mkdir()
    bind = _make_node_bin(tmp_path)
    proc = _run_verb(home, bind, "update-node 22\nID=$(id)")
    assert proc.returncode == 1, proc.stdout
    assert b"invalid major" in (proc.stdout + proc.stderr)


def test_update_node_requires_passwordless_sudo(tmp_path):
    """A valid major with no passwordless sudo fails cleanly (no partial install)."""
    home = tmp_path / "home"
    home.mkdir()
    bind = _make_node_bin(tmp_path, node_version="v18.19.1", sudo_ok=False)
    proc = _run_verb(home, bind, "update-node 22")
    assert proc.returncode == 1
    assert b"passwordless sudo unavailable" in (proc.stdout + proc.stderr)


def test_update_node_idempotent_when_current(tmp_path):
    """Already on the requested major → no-op success, install never attempted."""
    home = tmp_path / "home"
    home.mkdir()
    bind = _make_node_bin(tmp_path, node_version="v22.22.2", sudo_ok=True)
    proc = _run_verb(home, bind, "update-node 22")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["ok"] is True and data["note"] == "already-current", data


def test_update_node_verifies_by_node_version_not_apt(tmp_path):
    """The core safety property: success is decided by `node --version`, NOT the
    apt exit code. Here every install command 'succeeds' (stubbed sudo) but node
    stays on 18 → the verb must report a version mismatch, not a false success."""
    home = tmp_path / "home"
    home.mkdir()
    bind = _make_node_bin(tmp_path, node_version="v18.19.1", sudo_ok=True)
    proc = _run_verb(home, bind, "update-node 22")
    assert proc.returncode == 1, proc.stdout
    blob = proc.stdout + proc.stderr
    assert b"version mismatch after install" in blob, blob
