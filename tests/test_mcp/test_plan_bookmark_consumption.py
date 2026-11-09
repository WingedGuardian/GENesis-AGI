"""Exercise the actual helper, including separately scheduled OS readers."""

import asyncio
import fcntl
import inspect
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.caller_context import CallerContext, request_caller
from genesis.mcp import memory
from genesis.mcp.memory import _plan_bookmark as helper


@pytest.fixture
def pending_home(tmp_path, monkeypatch):
    home = tmp_path / "caller"
    pending = home / ".genesis/plan_bookmark_pending.json"
    pending.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "other-install"))
    return home, pending


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("state", ["absent", "complete", "malformed", "read-error", "create-error"])
async def test_pending_input_states(pending_home, monkeypatch, explicit, state):
    home, pending = pending_home
    if state != "absent":
        pending.write_text("{" if state == "malformed" else json.dumps({"title": "Owned plan", "session_id_hint": "short"}))
    manager = AsyncMock()
    manager.create_micro.return_value = "owned-bookmark"
    if state == "create-error":
        manager.create_micro.side_effect = RuntimeError("owned manager failure")
    if explicit:
        monkeypatch.setenv("HOME", str(home.parent / "server-decoy"))
    if state == "read-error":
        original = Path.read_text

        def fail_pending(path, *args, **kwargs):
            if path == pending:
                raise OSError("owned read failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_pending)
    kwargs = {"pending_file": pending} if explicit else {}
    await helper._process_plan_bookmark_pending(manager, lambda value: "resolved-" + value, **kwargs)
    assert manager.create_micro.await_count == int(state in {"complete", "create-error"})
    if manager.create_micro.await_count:
        assert manager.create_micro.call_args.kwargs["cc_session_id"] == "resolved-short"
        assert manager.create_micro.call_args.kwargs["source"] == "plan"
        assert manager.create_micro.call_args.kwargs["context_messages"][0]["text"] == "Owned plan"
    assert not pending.exists()
    if state == "absent":
        assert not pending.with_name(pending.name + ".lock").exists()


@pytest.mark.parametrize("state", ["busy", "open-error", "unlocked"])
async def test_lock_lifecycle_retains_one_inode(pending_home, state):
    _, pending = pending_home
    pending.write_text('{"title":"Owned plan"}')
    lock = pending.with_name(pending.name + ".lock")
    manager = AsyncMock()
    manager.create_micro.return_value = "owned-bookmark"
    if state == "open-error":
        lock.mkdir()
        held = None
    else:
        lock.write_text("existing inode")
        held = lock.open("a")
        if state == "busy":
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    inode = lock.stat().st_ino
    try:
        await helper._process_plan_bookmark_pending(manager, lambda value: value)
        assert pending.exists() is (state != "unlocked")
        assert manager.create_micro.await_count == int(state == "unlocked")
        assert lock.stat().st_ino == inode
        if state != "open-error":
            assert lock.read_text() == "existing inode"
    finally:
        if held is not None:
            held.close()
    if state == "busy":
        await helper._process_plan_bookmark_pending(manager, lambda value: value)
        assert not pending.exists() and manager.create_micro.await_count == 1
        assert lock.stat().st_ino == inode


def _child(home, result, genesis_home, *, ready=None, release=None):
    script = Path(__file__).with_name("plan_bookmark_consumer_child.py")
    argv = [sys.executable, str(script), "--source", helper.__file__, "--home", str(home), "--result", str(result), "--genesis-home", str(genesis_home)]
    passed = ()
    if ready is not None:
        argv += ["--ready", str(ready), "--release", str(release)]
        passed = (ready, release)
    return subprocess.Popen(argv, pass_fds=passed, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.mark.parametrize("kill_first", [False, True])
@pytest.mark.parametrize("separate_install", [False, True])
def test_two_process_readers_share_claim(pending_home, tmp_path, kill_first, separate_install):
    home, pending = pending_home
    pending.write_text('{"title":"Owned overlap"}')
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    first = _child(home, tmp_path / "first.json", tmp_path / "first-install", ready=ready_write, release=release_read)
    second = None
    try:
        os.close(ready_write)
        os.close(release_read)
        assert select.select([ready_read], [], [], 30)[0], "first helper did not reach its read"
        assert os.read(ready_read, 1) == b"1"
        if kill_first:
            first.kill()
            first.communicate(timeout=30)
        second = _child(home, tmp_path / "second.json", tmp_path / ("second-install" if separate_install else "first-install"))
        _, errors = second.communicate(timeout=30)
        assert second.returncode == 0, errors.decode()
        if not kill_first:
            assert pending.exists(), "busy reader consumed the first reader's input"
            os.write(release_write, b"1")
            _, errors = first.communicate(timeout=30)
            assert first.returncode == 0, errors.decode()
        results = [json.loads(p.read_text()) for p in (tmp_path / "first.json", tmp_path / "second.json") if p.exists()]
        assert len(results) == 1
        assert results[0]["context_messages"][0]["text"] == "Owned overlap"
        assert not pending.exists()
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=30)
        os.close(ready_read)
        os.close(release_write)


@pytest.mark.parametrize("state", ["unique", "ambiguous", "missing", "no-match", "full", "empty"])
def test_session_prefix_uses_selected_input_directory(pending_home, tmp_path, monkeypatch, state):
    home, pending = pending_home
    sessions = pending.parent / "sessions"
    full = "12345678-1234-4234-8234-123456789012"
    hint = full if state == "full" else "" if state == "empty" else "12345678"
    if state != "missing":
        sessions.mkdir()
        if state in {"unique", "ambiguous"}:
            (sessions / full).mkdir()
        if state == "ambiguous":
            (sessions / (full + "-other")).mkdir()
    decoy = tmp_path / "server/.genesis/sessions"
    decoy.mkdir(parents=True)
    (decoy / "12345678-server-decoy").mkdir()
    monkeypatch.setenv("HOME", str(decoy.parent.parent))
    assert memory._resolve_session_id(hint, sessions_dir=sessions) == (full if state == "unique" else hint)
    if state not in {"full", "empty"}:
        assert memory._resolve_session_id(hint) == "12345678-server-decoy"


@pytest.mark.parametrize("profile", [None, "codex", "external", "validator"])
async def test_guard_binds_checked_input_and_resolver(pending_home, tmp_path, monkeypatch, profile):
    _, pending = pending_home
    pending.write_text('{"title":"Guard path","session_id_hint":"12345678"}')
    full = "12345678-1234-4234-8234-123456789012"
    (pending.parent / "sessions" / full).mkdir(parents=True)
    monkeypatch.setattr(memory, "_PLAN_BOOKMARK_PENDING", pending)
    monkeypatch.setattr(memory, "_process_pending_bookmarks", True)
    for name in ("_store", "_retriever", "_db"):
        monkeypatch.setattr(memory, name, object())
    manager = AsyncMock()
    manager.create_micro.return_value = "owned-bookmark"
    monkeypatch.setattr(memory, "_bookmark_mgr", manager)
    jobs = []
    monkeypatch.setattr("genesis.db.request_database.spawn_database_task", lambda db, body, **kwargs: jobs.append(body))
    monkeypatch.setenv("HOME", str(tmp_path / "server-decoy"))
    try:
        with request_caller(CallerContext(profile=profile)):
            memory._require_init()
            assert len(jobs) == int(profile is None)
            for job in jobs:
                await job
        assert pending.exists() is (profile is not None)
        assert manager.create_micro.await_count == int(profile is None)
        if profile is None:
            assert manager.create_micro.call_args.kwargs["cc_session_id"] == full
    finally:
        for job in jobs:
            job.close()


@pytest.mark.parametrize("waiting_for_unit", [False, True])
async def test_unentered_owned_job_keeps_pending_input(pending_home, tmp_path, waiting_for_unit):
    from genesis.db.request_database import RequestDatabase

    _, pending = pending_home
    pending.write_text('{"title":"Unentered job"}')
    path = tmp_path / "owned.db"
    path.touch()
    db = RequestDatabase(path, max_units=1, max_background_units=1, max_background_jobs=2)
    manager = AsyncMock()
    body = helper._process_plan_bookmark_pending(manager, lambda value: value, pending_file=pending)
    held = False
    try:
        if waiting_for_unit:
            await db._background_slots.acquire()
            held = True
        task = db.spawn(body)
        if waiting_for_unit:
            await asyncio.sleep(0)
            assert task in db._owners
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert inspect.getcoroutinestate(body) == inspect.CORO_CLOSED
        assert pending.read_text() == '{"title":"Unentered job"}'
        assert not pending.with_name(pending.name + ".lock").exists()
        manager.create_micro.assert_not_awaited()
        assert not db._jobs and not db._owners
    finally:
        if held:
            db._background_slots.release()
        await db.drain(grace_s=0)
        body.close()


async def test_claim_released_before_manager_await(pending_home):
    _, pending = pending_home
    pending.write_text('{"title":"First plan"}')
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Manager:
        async def create_micro(self, **kwargs):
            title = kwargs["context_messages"][0]["text"]
            seen.append(title)
            if title == "First plan":
                entered.set()
                await release.wait()
            return "owned-bookmark"

    first = asyncio.create_task(helper._process_plan_bookmark_pending(Manager(), lambda value: value))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        pending.write_text('{"title":"Second plan"}')
        await helper._process_plan_bookmark_pending(Manager(), lambda value: value)
        assert seen == ["First plan", "Second plan"]
        assert not pending.exists()
    finally:
        release.set()
        await first
