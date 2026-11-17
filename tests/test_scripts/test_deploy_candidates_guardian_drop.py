"""The Guardian's drop (#2978) against the REAL engine: the commands
src/genesis/guardian/recovery.py sends into the container run here through the
real `scripts/deploy_candidates` entry in a scratch `live` checkout, from a
plain shell (`env -i`, HOME only), the way `su - ubuntu -c` runs them. Only the
container hop and the server restart are stood in for."""

from __future__ import annotations

import fcntl
import os
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian import recovery
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig
from genesis.guardian.recovery import RecoveryEngine, drop_target, live_candidates
from genesis.guardian.snapshots import SnapshotManager
from genesis.guardian.state_machine import ConfirmationStateMachine


def _two_live(w, dc) -> str:
    w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    return hb


def _container(w, calls: list[str]):
    """`incus exec <c> -- su - ubuntu -c <cmd>`, run as a plain local shell."""
    os.symlink(w.root, w.home / "genesis")  # the checkout at ~/genesis

    async def run(*args, timeout=10.0, **kwargs):
        cmd = args[-1]
        calls.append(cmd)
        if "systemctl" in cmd or "journalctl" in cmd:
            return (0, "", "")
        p = subprocess.run(
            ["env", "-i", f"HOME={w.home}", "GIT_CONFIG_NOSYSTEM=1", "/bin/bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (p.returncode, p.stdout.strip(), p.stderr.strip())

    return run


def _engine() -> RecoveryEngine:
    config = GuardianConfig()
    d = AlertDispatcher()
    d.add_channel(AsyncMock())
    return RecoveryEngine(config, ConfirmationStateMachine(config), SnapshotManager(config), d)


@pytest.fixture
def server_comes_up():
    seen = []

    async def fake(container):
        seen.append(container)
        return {"ActiveState": "active", "SubState": "running", "InvocationID": str(len(seen))}

    with patch("genesis.guardian.recovery._server_unit", fake):
        yield


@pytest.mark.asyncio
async def test_the_guardian_drops_the_newest_candidate_through_the_engine(
    dc, dc_ready, server_comes_up
):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    calls: list[str] = []
    run = _container(w, calls)
    with (
        patch("genesis.guardian.recovery._run_subprocess", run),
        patch("genesis.guardian.live_drop._run_subprocess", run),
    ):
        data = await live_candidates("genesis")
        assert data is not None, calls
        target, why = drop_target(data)
        assert target == ("feat/b", w.rev("refs/heads/feat/b"), w.rev("refs/heads/live")), why
        ok, detail = await _engine()._revert_code("genesis", target)
    assert ok is True, detail
    assert [b for b, _ in w.live_merges()] == ["feat/a"]
    assert [c["branch"] for c in w.manifest()["candidates"]] == ["feat/a"]
    assert w.git(w.root, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert w.git(w.root, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/live"
    assert any("systemctl --user restart" in c for c in calls)
    assert not (w.home / ".genesis" / "update_in_progress.pid").exists()


@pytest.mark.asyncio
async def test_a_held_update_lock_drops_nothing_and_restarts_nothing(
    dc, dc_ready, server_comes_up, monkeypatch
):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    monkeypatch.setattr(recovery, "_DROP_WAIT_S", 1)
    lock = w.home / ".genesis" / "locks" / "update.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o644)
    calls: list[str] = []
    run = _container(w, calls)
    try:
        with (
            patch("genesis.guardian.recovery._run_subprocess", run),
            patch("genesis.guardian.live_drop._run_subprocess", run),
        ):
            target, _ = drop_target(await live_candidates("genesis"))
            fcntl.flock(fd, fcntl.LOCK_EX)
            held = await recovery.update_lock_holder("genesis")
            ok, detail = await _engine()._revert_code("genesis", target)
    finally:
        os.close(fd)
    assert held == (True, "")
    assert ok is False and "update.lock was held for 1 s" in detail, detail
    assert [b for b, _ in w.live_merges()] == ["feat/a", "feat/b"]
    assert not any("systemctl" in c for c in calls)
