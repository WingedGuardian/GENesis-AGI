"""Tests for the guardian-side offline-bundle watch (F.4, guardian/bundle_watch.py).

Covers the host-only archive hop (refuse stamp-less/empty, keep-N prune never
below one) and the freshness alert (stale WARN, damped realert, recovery INFO,
never-configured no-warn), using a real GuardianConfig + a capturing dispatcher.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.guardian import bundle_watch
from genesis.guardian.alert.base import AlertSeverity
from genesis.guardian.bundle_watch import (
    bundle_archive_status,
    check_repo_bundle_and_alert,
)
from genesis.guardian.config import GuardianConfig, RepoBundleConfig


class _FakeDispatcher:
    def __init__(self):
        self.sent = []

    async def send(self, alert):
        self.sent.append(alert)
        return True


def _config(tmp_path: Path, **cfg_kw) -> GuardianConfig:
    config = GuardianConfig(state_dir=str(tmp_path))
    config.repo_bundle = RepoBundleConfig(
        **{"keep": 3, "stale_days": 3.0, "realert_hours": 24.0, **cfg_kw}
    )
    return config


def _source_dir(config: GuardianConfig) -> Path:
    return config.shared_path / "guardian" / "repo-bundle"


def _write_publish(
    source_dir: Path, head: str, *, verified_at: str | None = None, data: bytes = b"BUNDLEDATA"
) -> str:
    source_dir.mkdir(parents=True, exist_ok=True)
    name = f"genesis-{head[:12]}.bundle"
    (source_dir / name).write_bytes(data)
    now = verified_at or datetime.now(UTC).isoformat()
    (source_dir / "BUNDLE_STAMP").write_text(
        json.dumps(
            {
                "version": 1,
                "head": head,
                "bundle": name,
                "size": len(data),
                "sha256": "deadbeef",
                "created_at": now,
                "last_verified_at": now,
            }
        )
    )
    return name


def _write_archive_stamp(archive_dir: Path, last_verified_at: str, bundle="genesis-x.bundle"):
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / bundle).write_bytes(b"x")
    (archive_dir / "BUNDLE_STAMP").write_text(
        json.dumps(
            {
                "version": 1,
                "head": "x" * 40,
                "bundle": bundle,
                "size": 1,
                "sha256": "x",
                "created_at": last_verified_at,
                "last_verified_at": last_verified_at,
            }
        )
    )


# ── archive hop ────────────────────────────────────────────────────────────


def test_archive_copies_newest_bundle_and_stamp(tmp_path):
    config = _config(tmp_path)
    source = _source_dir(config)
    name = _write_publish(source, "abc123def4567")
    archive = config.state_path / "repo-archive"

    bundle_watch._archive_bundles(config.repo_bundle, source, archive)

    assert (archive / name).exists()
    assert (archive / "BUNDLE_STAMP").exists()
    assert (archive.stat().st_mode & 0o777) == 0o700


def test_archive_refuses_stampless_source(tmp_path):
    config = _config(tmp_path)
    source = _source_dir(config)
    source.mkdir(parents=True)
    (source / "genesis-orphan.bundle").write_bytes(b"x")  # bundle but NO stamp
    archive = config.state_path / "repo-archive"

    bundle_watch._archive_bundles(config.repo_bundle, source, archive)
    assert not archive.exists() or not list(archive.glob("genesis-*.bundle"))


def test_archive_rejects_traversal_bundle_name(tmp_path):
    """Codex P1: a malicious stamp (container-writable) naming an escape path must
    NOT make the host guardian read/write outside the archive."""
    config = _config(tmp_path)
    source = _source_dir(config)
    source.mkdir(parents=True)
    for evil in ("../../evil.bundle", "/tmp/evil.bundle", "genesis-XYZ.bundle"):
        (source / "BUNDLE_STAMP").write_text(
            json.dumps(
                {
                    "version": 1,
                    "head": "a" * 40,
                    "bundle": evil,
                    "size": 1,
                    "sha256": "x",
                    "created_at": "t",
                    "last_verified_at": "t",
                }
            )
        )
        archive = config.state_path / "repo-archive"
        bundle_watch._archive_bundles(config.repo_bundle, source, archive)
        assert not (tmp_path / "evil.bundle").exists()
        assert not (Path("/tmp/evil.bundle")).exists()
        assert not archive.exists() or not list(archive.glob("*.bundle"))


def test_archive_noop_on_absent_source(tmp_path):
    config = _config(tmp_path)
    archive = config.state_path / "repo-archive"
    # Source dir never created → clean no-op, no crash.
    bundle_watch._archive_bundles(config.repo_bundle, _source_dir(config), archive)
    assert not archive.exists()


def test_prune_keeps_n_never_below_one(tmp_path):
    archive = tmp_path / "repo-archive"
    archive.mkdir()
    # Five bundles with staggered mtimes.
    for i in range(5):
        f = archive / f"genesis-{i:012d}.bundle"
        f.write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))
    bundle_watch._prune_archive(archive, keep=3)
    remaining = sorted(f.name for f in archive.glob("genesis-*.bundle"))
    # Newest 3 by mtime kept (indices 2,3,4).
    assert remaining == [
        "genesis-000000000002.bundle",
        "genesis-000000000003.bundle",
        "genesis-000000000004.bundle",
    ]


def test_archive_prune_integration_keeps_keep(tmp_path):
    """Successive publishes of different heads accumulate in the archive, pruned
    to keep=2."""
    config = _config(tmp_path, keep=2)
    source = _source_dir(config)
    archive = config.state_path / "repo-archive"
    for i, head in enumerate((b"a", b"b", b"c")):
        name = _write_publish(source, head.decode() * 40)
        # Stagger the SOURCE bundle mtime; _atomic_copy preserves it, so archive
        # prune order is deterministic (newest-by-mtime kept).
        os.utime(source / name, (1000 + i, 1000 + i))
        bundle_watch._archive_bundles(config.repo_bundle, source, archive)
    bundles = list(archive.glob("genesis-*.bundle"))
    assert len(bundles) == 2


# ── freshness alert ─────────────────────────────────────────────────────────


async def test_freshness_warns_when_stale_then_damps(tmp_path):
    config = _config(tmp_path, stale_days=3.0)
    archive = config.state_path / "repo-archive"
    old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    _write_archive_stamp(archive, old)
    disp = _FakeDispatcher()

    await check_repo_bundle_and_alert(config, disp)
    assert len(disp.sent) == 1
    assert disp.sent[0].severity == AlertSeverity.WARNING

    # Second call immediately → within realert window → no new alert.
    await check_repo_bundle_and_alert(config, disp)
    assert len(disp.sent) == 1


async def test_freshness_recovers_with_info(tmp_path):
    config = _config(tmp_path, stale_days=3.0)
    archive = config.state_path / "repo-archive"
    disp = _FakeDispatcher()

    _write_archive_stamp(archive, (datetime.now(UTC) - timedelta(days=5)).isoformat())
    await check_repo_bundle_and_alert(config, disp)
    assert len(disp.sent) == 1  # WARNING

    # Fresh stamp → recovery INFO.
    _write_archive_stamp(archive, datetime.now(UTC).isoformat())
    await check_repo_bundle_and_alert(config, disp)
    assert len(disp.sent) == 2
    assert disp.sent[1].severity == AlertSeverity.INFO


async def test_freshness_no_alert_when_never_configured(tmp_path):
    config = _config(tmp_path)
    disp = _FakeDispatcher()
    # No archive stamp at all → never-configured install → silence.
    await check_repo_bundle_and_alert(config, disp)
    assert disp.sent == []


async def test_disabled_is_silent(tmp_path):
    config = _config(tmp_path, enabled=False, stale_days=3.0)
    archive = config.state_path / "repo-archive"
    _write_archive_stamp(archive, (datetime.now(UTC) - timedelta(days=10)).isoformat())
    disp = _FakeDispatcher()
    await check_repo_bundle_and_alert(config, disp)
    assert disp.sent == []


# ── status verb helper ──────────────────────────────────────────────────────


def test_bundle_archive_status_shape(tmp_path):
    config = _config(tmp_path)
    archive = config.state_path / "repo-archive"
    _write_archive_stamp(archive, datetime.now(UTC).isoformat(), bundle="genesis-abc.bundle")
    status = bundle_archive_status(config)
    assert status["ok"] is True
    assert status["action"] == "bundle-status"
    assert status["count"] == 1
    assert status["bundles"][0]["name"] == "genesis-abc.bundle"
    assert status["stamp"]["bundle"] == "genesis-abc.bundle"


def test_bundle_archive_status_empty(tmp_path):
    config = _config(tmp_path)
    status = bundle_archive_status(config)
    assert status["ok"] is True
    assert status["count"] == 0
    assert status["stamp"] is None


def test_import_is_host_guardian_venv_safe():
    """Regression: bundle_watch + repo_bundle run in the MINIMAL host guardian
    venv (no aiohttp). Importing them must NOT eagerly pull
    genesis.observability (whose __init__ loads the aiohttp health chain) —
    check_git_cheap is lazy-imported inside publish (a container-only path).

    Run in a fresh subprocess so import-cache state from other tests can't mask a
    regression. A live E2E on the host guardian caught this the hard way (2026-07-15):
    the every-30s tick threw ModuleNotFoundError: aiohttp and the archive never ran.
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import genesis.guardian.bundle_watch\n"
        "import genesis.guardian.repo_bundle\n"
        "bad = [m for m in ('genesis.observability.git_health', 'aiohttp') "
        "if m in sys.modules]\n"
        "assert not bad, f'host-unsafe eager imports: {bad}'\n"
        "print('ok')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
