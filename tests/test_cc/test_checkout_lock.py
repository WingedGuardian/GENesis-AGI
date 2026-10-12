from __future__ import annotations

import asyncio
import fcntl
import threading

import pytest

from genesis.cc import checkout_lock
from tests.test_scripts._checkout_lock_helpers import can_lock, git, held, repo

_REAL_CHECKOUT_LOCK_PATH = checkout_lock.checkout_lock_path


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "genesis-checkout.lock"


@pytest.mark.asyncio
async def test_shared_admissions_coexist(lock_path):
    first = await checkout_lock.admit_launch()
    second = await checkout_lock.admit_launch()
    assert not can_lock(lock_path, fcntl.LOCK_EX)
    first.release()
    second.release()
    assert can_lock(lock_path, fcntl.LOCK_EX)


@pytest.mark.asyncio
async def test_exclusive_holder_blocks_admission_until_released(lock_path):
    with held(lock_path, fcntl.LOCK_EX):
        pending = asyncio.create_task(checkout_lock.admit_launch())
        await asyncio.sleep(0.5)
        assert not pending.done()
    admission = await asyncio.wait_for(pending, 2)
    admission.release()


@pytest.mark.asyncio
async def test_cancelled_wait_closes_its_fd(lock_path):
    with held(lock_path, fcntl.LOCK_EX):
        pending = asyncio.create_task(checkout_lock.admit_launch())
        await asyncio.sleep(0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert can_lock(lock_path, fcntl.LOCK_EX)


@pytest.mark.asyncio
async def test_release_is_idempotent(lock_path):
    admission = await checkout_lock.admit_launch()
    admission.release()
    admission.release()
    assert can_lock(lock_path, fcntl.LOCK_EX)


@pytest.mark.asyncio
async def test_child_does_not_inherit_admission_fd(lock_path):
    admission = await checkout_lock.admit_launch()
    child = await asyncio.create_subprocess_exec("sleep", "30")
    admission.release()
    assert child.returncode is None
    assert can_lock(lock_path, fcntl.LOCK_EX)
    child.kill()
    await child.wait()


@pytest.mark.asyncio
async def test_long_wait_logs_once_and_keeps_waiting(lock_path, monkeypatch, caplog):
    monkeypatch.setattr(checkout_lock, "LONG_WAIT_WARN_S", 0.01)
    monkeypatch.setattr(checkout_lock, "_POLL_S", 0.002)
    with held(lock_path, fcntl.LOCK_EX), caplog.at_level(
        "ERROR", logger=checkout_lock.__name__
    ):
        pending = asyncio.create_task(checkout_lock.admit_launch())
        for _ in range(100):
            if "checkout mutation has held" in caplog.text:
                break
            await asyncio.sleep(0.01)
        assert "checkout mutation has held" in caplog.text
    admission = await asyncio.wait_for(pending, 2)
    admission.release()
    assert caplog.text.count("checkout mutation has held") == 1
    assert str(lock_path) in caplog.text


def test_common_dir_is_shared_by_main_and_linked_worktree(tmp_path):
    root = repo(tmp_path)
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "file").write_text("x")
    git(root, "add", "file")
    git(root, "commit", "-qm", "init")
    linked = tmp_path / "linked"
    git(root, "worktree", "add", "-qb", "linked", str(linked))
    expected = root / ".git" / "genesis-checkout.lock"
    assert checkout_lock._lock_path_for(root) == expected
    assert checkout_lock._lock_path_for(linked) == expected


@pytest.mark.asyncio
async def test_non_git_checkout_fails_open_once(tmp_path, monkeypatch, caplog):
    from genesis import env

    non_git = tmp_path / "not-git"
    non_git.mkdir()
    assert checkout_lock._lock_path_for(non_git) is None
    lock_path = tmp_path / "recovered.lock"
    resolution_threads = []

    def resolve(_root):
        resolution_threads.append(threading.get_ident())
        return None if len(resolution_threads) == 1 else lock_path

    monkeypatch.setattr(env, "repo_root", lambda: non_git)
    monkeypatch.setattr(checkout_lock, "_lock_path_for", resolve)
    monkeypatch.setattr(checkout_lock, "checkout_lock_path", _REAL_CHECKOUT_LOCK_PATH)
    monkeypatch.setattr(checkout_lock, "_WARNED_FAIL_OPEN", False)
    main_thread = threading.get_ident()
    with caplog.at_level("WARNING", logger=checkout_lock.__name__):
        first = await checkout_lock.admit_launch()
        second = await checkout_lock.admit_launch()
    first.release()
    second.release()
    assert caplog.text.count("fail-open") == 1
    assert len(resolution_threads) == 2
    assert all(thread_id != main_thread for thread_id in resolution_threads)
