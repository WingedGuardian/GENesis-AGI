"""Tests for the offline git-bundle publish (F.4, guardian/repo_bundle.py).

Uses real scratch git repos + a scratch shared dir (no mocks of git) so the
bundle create/verify/clone behavior is exercised for real — the one thing the
spike proved and these lock in as regressions.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from genesis.guardian import repo_bundle
from genesis.guardian.repo_bundle import publish_repo_bundle


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _make_repo(path: Path, content: str = "hello") -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "f.txt").write_text(content)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return _git(path, "rev-parse", "HEAD").strip()


def _bundle_dir(shared: Path) -> Path:
    return shared / "guardian" / "repo-bundle"


async def test_publish_creates_verifiable_bundle(tmp_path):
    repo = tmp_path / "repo"
    head = _make_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()

    result = await publish_repo_bundle(repo=repo, shared_dir=shared)
    assert result and result["action"] == "published"
    assert result["head"] == head

    bdir = _bundle_dir(shared)
    bundle = bdir / result["bundle"]
    assert bundle.exists()
    # The published bundle actually verifies as a real git bundle.
    rc = subprocess.run(
        ["git", "-C", str(repo), "bundle", "verify", str(bundle)],
        capture_output=True,
    ).returncode
    assert rc == 0

    stamp = json.loads((bdir / "BUNDLE_STAMP").read_text())
    assert stamp["head"] == head
    assert stamp["bundle"] == result["bundle"]
    assert stamp["sha256"] and stamp["size"] > 0
    assert stamp["created_at"] == stamp["last_verified_at"]
    # 0600 on the bundle, 0700 on the dir.
    assert (bundle.stat().st_mode & 0o777) == 0o600


async def test_reclone_matches_head(tmp_path):
    """The actual lifeline: a clone from the published bundle checks out the
    exact container HEAD."""
    repo = tmp_path / "repo"
    head = _make_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()

    result = await publish_repo_bundle(repo=repo, shared_dir=shared)
    bundle = _bundle_dir(shared) / result["bundle"]

    dest = tmp_path / "reclone"
    subprocess.run(["git", "clone", "-q", str(bundle), str(dest)], check=True)
    re_head = _git(dest, "rev-parse", "HEAD").strip()
    assert re_head == head


async def test_skips_unchanged_head_but_refreshes_verified_at(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()

    first = await publish_repo_bundle(repo=repo, shared_dir=shared)
    assert first["action"] == "published"
    stamp1 = json.loads((_bundle_dir(shared) / "BUNDLE_STAMP").read_text())
    bundle_mtime1 = (_bundle_dir(shared) / first["bundle"]).stat().st_mtime_ns

    second = await publish_repo_bundle(repo=repo, shared_dir=shared)
    assert second["action"] == "verified_unchanged"
    stamp2 = json.loads((_bundle_dir(shared) / "BUNDLE_STAMP").read_text())
    # Bundle content NOT rebuilt (same file untouched)...
    assert (_bundle_dir(shared) / first["bundle"]).stat().st_mtime_ns == bundle_mtime1
    # ...but last_verified_at advanced (created_at unchanged).
    assert stamp2["created_at"] == stamp1["created_at"]
    assert stamp2["last_verified_at"] >= stamp1["last_verified_at"]


async def test_force_rebuilds_unchanged(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()

    await publish_repo_bundle(repo=repo, shared_dir=shared)
    forced = await publish_repo_bundle(repo=repo, shared_dir=shared, force=True)
    assert forced["action"] == "published"


async def test_new_commit_publishes_and_prunes_shared_to_one(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()

    r1 = await publish_repo_bundle(repo=repo, shared_dir=shared)
    # New commit → new HEAD → new bundle; shared side keeps only the newest.
    (repo / "f2.txt").write_text("more")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "second")
    r2 = await publish_repo_bundle(repo=repo, shared_dir=shared)
    assert r2["action"] == "published"
    assert r2["bundle"] != r1["bundle"]
    bundles = list(_bundle_dir(shared).glob("genesis-*.bundle"))
    assert len(bundles) == 1
    assert bundles[0].name == r2["bundle"]


async def test_health_gate_refuses_unhealthy_repo(tmp_path):
    """A zeroed .git/config makes check_git_cheap fail → publish refuses and the
    last good bundle is left untouched."""
    repo = tmp_path / "repo"
    _make_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()

    good = await publish_repo_bundle(repo=repo, shared_dir=shared)
    good_bundle = _bundle_dir(shared) / good["bundle"]
    good_sha_before = repo_bundle._sha256(good_bundle)

    # Corrupt the config the way the outage did (null-fill).
    cfg = repo / ".git" / "config"
    cfg.write_bytes(b"\x00" * cfg.stat().st_size)

    result = await publish_repo_bundle(repo=repo, shared_dir=shared, force=True)
    assert result["action"] == "refused"
    assert result["reason"] == "git_unhealthy"
    # Last good bundle untouched.
    assert good_bundle.exists()
    assert repo_bundle._sha256(good_bundle) == good_sha_before


async def test_no_shared_mount_returns_none(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    result = await publish_repo_bundle(repo=repo, shared_dir=tmp_path / "absent")
    assert result is None


async def test_insufficient_space_refuses(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()

    import shutil as _shutil

    class _DU:
        free = 1  # 1 byte free — well under 2x the store

    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: _DU())
    result = await publish_repo_bundle(repo=repo, shared_dir=shared)
    assert result["action"] == "refused"
    assert result["reason"] == "insufficient_space"


def test_cli_main_no_shared_mount(tmp_path, monkeypatch, capsys):
    """CLI entrypoint exits 0 and prints JSON when there's no shared mount."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "nope"))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path / "repo"))
    _make_repo(tmp_path / "repo")
    rc = repo_bundle.main(["--force"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] in ("skipped", "published", "refused", "verified_unchanged")
