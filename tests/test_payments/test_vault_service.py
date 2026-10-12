"""Tests for the vault service: in-memory key, Unix socket, audit trail."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import time
from pathlib import Path

import aiosqlite
import pytest
from pyrage import x25519

from genesis.db.crud import payment_vault_events as audit
from genesis.payments import vault_service as svc_mod
from genesis.payments import vault_store as vs
from genesis.payments.vault_service import VaultService

PASSPHRASE = "quartz lantern ember noodle 41"
MIG = importlib.import_module("genesis.db.migrations.20261011060631_payment_vault_events")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def _sealed(tmp_path_factory):
    path = tmp_path_factory.mktemp("sealed") / "payment_vault"
    vs.init_vault(PASSPHRASE, path)
    return path


@pytest.fixture
def vault(_sealed, tmp_path):
    path = tmp_path / "payment_vault"
    shutil.copytree(_sealed, path)
    return path


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        await MIG.up(conn)
        yield conn


def _make(db, vault, tmp_path, **kw):
    return VaultService(db, vault_path=vault, socket_path=tmp_path / "run" / "v.sock", **kw)


@pytest.fixture
async def service(db, vault, tmp_path):
    svc = _make(db, vault, tmp_path)
    await svc.start()
    yield svc
    await svc.stop()


async def _ask(svc, payload, raw: bytes | None = None) -> dict | None:
    reader, writer = await asyncio.open_unix_connection(str(svc._socket_path))
    writer.write(raw if raw is not None else (json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line) if line else None


def _rebind(vault: Path) -> None:
    """Re-record the key pair in vault_meta after a test altered a key file.

    vault_store's check_ready refuses a key file that is not the pair init
    recorded, before the service ever unwraps. These tests exercise the
    service's own checks behind it, so they make check_ready pass first.
    """
    import hashlib

    digest = hashlib.sha256((vault / vs.IDENTITY).read_bytes()).hexdigest()
    recipient = (vault / vs.RECIPIENT).read_text().strip()
    with sqlite3.connect(vault / vs.CARDS) as conn:
        conn.executemany(
            "UPDATE vault_meta SET value = ? WHERE key = ?",
            [(digest, "identity_sha256"), (recipient, "recipient")],
        )


async def _events(db) -> list[dict]:
    return list(reversed(await audit.list_recent(db, 100)))


class TestSocket:
    async def test_private_socket_and_start_event(self, service, db, tmp_path):
        assert stat.S_IMODE(os.lstat(tmp_path / "run").st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(service._socket_path).st_mode) == 0o600
        assert (await _events(db))[0]["event"] == "service_start"

    async def test_stop_unlinks_the_socket_and_logs(self, db, vault, tmp_path):
        svc = _make(db, vault, tmp_path)
        await svc.start()
        await svc.stop()
        assert not svc._socket_path.exists()
        assert (await _events(db))[-1]["event"] == "service_stop"

    async def test_a_stale_socket_from_a_killed_server_is_replaced(self, db, vault, tmp_path):
        path = tmp_path / "run" / "v.sock"
        path.parent.mkdir(mode=0o700)
        dead = socket.socket(socket.AF_UNIX)
        dead.bind(str(path))
        dead.close()
        svc = _make(db, vault, tmp_path)
        await svc.start()
        assert (await _ask(svc, {"op": "status"}))["ok"]
        await svc.stop()

    async def test_a_foreign_file_at_the_path_is_never_replaced(self, db, vault, tmp_path):
        path = tmp_path / "run" / "v.sock"
        path.parent.mkdir(mode=0o700)
        path.write_text("not ours")
        with pytest.raises(OSError):
            await _make(db, vault, tmp_path).start()
        assert path.read_text() == "not ours"

    async def test_stop_leaves_a_replaced_path_alone(self, db, vault, tmp_path):
        svc = _make(db, vault, tmp_path)
        await svc.start()
        svc._socket_path.unlink()
        svc._socket_path.write_text("someone else's")
        await svc.stop()
        assert svc._socket_path.read_text() == "someone else's"

    async def test_an_idle_client_does_not_hold_up_stop(self, db, vault, tmp_path):
        svc = _make(db, vault, tmp_path)
        await svc.start()
        _reader, writer = await asyncio.open_unix_connection(str(svc._socket_path))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await svc.stop()
        assert time.monotonic() - started < 2
        writer.close()

    async def test_another_uid_is_refused_and_logged(self, service, db, monkeypatch):
        monkeypatch.setattr(svc_mod, "peer_of", lambda w: {"pid": 1, "uid": os.getuid() + 1})
        assert await _ask(service, {"op": "status"}) is None
        assert (await _events(db))[-1]["event"] == "peer_refused"

    @pytest.mark.parametrize(
        "raw",
        [
            b"x" * 5000 + b"\n",
            b"not json\n",
            b'["status"]\n',
            b'{"op": "rm"}\n',
            b'{"op": "unlock"}\n',
            b'{"op": "unlock", "passphrase": ""}\n',
        ],
    )
    async def test_bad_requests_get_a_fixed_answer(self, service, raw):
        reply = await _ask(service, None, raw=raw)
        assert reply == {"ok": False, "error": "bad request"}

    async def test_the_peer_is_recorded(self, service, db):
        await _ask(service, {"op": "lock"})
        await _ask(service, {"op": "unlock", "passphrase": "not it"})
        refused = (await _events(db))[-1]
        assert refused["event"] == "unlock_refused"
        peer = refused["detail"]["peer"]
        assert peer["pid"] == os.getpid() and peer["uid"] == os.getuid()
        assert peer["comm"] and peer["parent"] and isinstance(peer["start"], int)


class TestUnlockAndLock:
    async def test_full_cycle(self, service, db):
        assert (await _ask(service, {"op": "status"}))["unlocked"] is False
        wrong = await _ask(service, {"op": "unlock", "passphrase": "not it"})
        assert wrong == {"ok": False, "error": "wrong passphrase (or a damaged identity.age)"}
        ok = await _ask(service, {"op": "unlock", "passphrase": PASSPHRASE})
        assert ok["ok"] and ok["state"] == "unlocked" and ok["relock_in_s"] == 1800
        status = await _ask(service, {"op": "status"})
        assert status["unlocked"] and status["relock_in_s"] <= 1800
        assert status["last_event"]["event"] == "unlock"
        again = await _ask(service, {"op": "unlock", "passphrase": "anything"})
        assert again == {"ok": True, "state": "already unlocked"}
        assert (await _ask(service, {"op": "lock"}))["state"] == "locked"
        assert (await _ask(service, {"op": "lock"}))["state"] == "already locked"
        events = [e["event"] for e in await _events(db)]
        # The second lock had nothing to drop and is on the trail anyway.
        assert events == ["service_start", "unlock_refused", "unlock", "lock", "lock"]

    async def test_no_reply_or_event_holds_the_passphrase(self, service, db):
        replies = [
            await _ask(service, {"op": "unlock", "passphrase": PASSPHRASE + "x"}),
            await _ask(service, {"op": "unlock", "passphrase": PASSPHRASE}),
            await _ask(service, {"op": "status"}),
        ]
        rows = await _events(db)
        assert "quartz" not in json.dumps(replies) + json.dumps(rows)

    async def test_the_unwrap_child_gets_an_empty_environment(self, service, monkeypatch):
        seen = {}
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            seen.update(kwargs, argv=args)
            return await real(*args, **kwargs)

        monkeypatch.setenv("SOME_PROVIDER_API_KEY", "not-for-the-child")
        monkeypatch.setattr(svc_mod.asyncio, "create_subprocess_exec", spy)
        assert (await service.unlock(PASSPHRASE, None))["ok"]
        assert seen["env"] == {}
        assert seen["argv"][1] == "-I"
        assert not any(PASSPHRASE in str(arg) for arg in seen["argv"])  # stdin only

    async def test_unlock_keeps_the_event_loop_responsive(self, service):
        lag, done = 0.0, asyncio.Event()

        async def ticker():
            nonlocal lag
            while not done.is_set():
                t = time.perf_counter()
                await asyncio.sleep(0.01)
                lag = max(lag, time.perf_counter() - t - 0.01)

        tick = asyncio.create_task(ticker())
        assert (await service.unlock(PASSPHRASE, None))["ok"]
        done.set()
        await tick
        assert lag < 0.25, f"loop stalled {lag:.2f}s during unlock"

    async def test_idle_relock(self, db, vault, tmp_path):
        svc = _make(db, vault, tmp_path, idle_relock_s=0.2)
        await svc.start()
        assert (await svc.unlock(PASSPHRASE, None))["ok"]
        await asyncio.sleep(0.5)
        assert not svc.unlocked
        assert (await _events(db))[-1]["event"] == "idle_relock"
        await svc.stop()

    async def test_a_lock_during_an_unlock_wins(self, service):
        pending = asyncio.create_task(service.unlock(PASSPHRASE, None))
        await asyncio.sleep(0.2)  # the unwrap child runs for seconds
        await service.lock(None)
        result = await pending
        assert not result["ok"] and "locked during the unlock" in result["error"]
        assert not service.unlocked

    async def test_a_stop_during_an_unlock_wins(self, db, vault, tmp_path):
        svc = _make(db, vault, tmp_path)
        await svc.start()
        pending = asyncio.create_task(svc.unlock(PASSPHRASE, None))
        await asyncio.sleep(0.2)
        await svc.stop()
        assert not (await pending)["ok"]
        assert not svc.unlocked

    async def test_a_client_that_left_is_not_unlocked_for(self, service):
        result = await service.unlock(PASSPHRASE, None, client_gone=lambda: True)
        assert not result["ok"] and not service.unlocked

    async def test_a_client_that_disconnects_mid_unlock_is_not_unlocked_for(self, service, db):
        reader, writer = await asyncio.open_unix_connection(str(service._socket_path))
        writer.write((json.dumps({"op": "unlock", "passphrase": PASSPHRASE}) + "\n").encode())
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()  # Ctrl-C, or the CLI giving up
        for _ in range(100):  # the unwrap child takes a few seconds
            await asyncio.sleep(0.1)
            if (await _events(db))[-1]["event"] != "service_start":
                break
        assert not service.unlocked
        last = (await _events(db))[-1]
        assert last["event"] == "unlock_refused" and last["detail"]["reason"] == "the client left"

    @pytest.mark.parametrize("interrupt", ["lock", "stop"])
    async def test_a_lock_or_stop_during_the_audit_write_wins(
        self, service, db, monkeypatch, interrupt
    ):
        real = audit.record_event
        racers: list[asyncio.Task] = []

        async def racing(conn, event, detail=None):
            if event == "unlock":
                # A separate request, as in production: it takes effect (epoch
                # bump, key dropped) at once and its own audit row queues
                # behind this write. Awaiting it here would wait on the audit
                # lock this write holds.
                racers.append(
                    asyncio.create_task(
                        service.lock(None) if interrupt == "lock" else service.stop()
                    )
                )
                await asyncio.sleep(0)
            await real(conn, event, detail)

        monkeypatch.setattr(audit, "record_event", racing)
        result = await asyncio.wait_for(service.unlock(PASSPHRASE, None), 30)
        await asyncio.wait_for(asyncio.gather(*racers), 30)
        assert not result["ok"] and interrupt.rstrip("p") in result["error"]
        assert not service.unlocked and service._relock_handle is None
        assert service._unlocked_at is None
        unlock_trail = [
            e["event"] for e in await _events(db) if e["event"] in ("unlock", "unlock_refused")
        ]
        assert unlock_trail[-2:] == ["unlock", "unlock_refused"], unlock_trail  # ends true

    async def test_stale_identity_relocks(self, service, vault, db):
        assert (await service.unlock(PASSPHRASE, None))["ok"]
        (vault / vs.RECIPIENT).write_text(str(x25519.Identity.generate().to_public()) + "\n")
        status = await service.status()
        assert status["unlocked"] is False
        assert (await _events(db))[-1]["event"] == "stale_identity_relock"


class TestBoundedReads:
    """The service reads vault files as the store does: capped, never through a symlink."""

    @pytest.mark.parametrize("shape", ["oversized", "symlink"])
    def test_the_unwrap_child_refuses_without_reading_the_whole_file(self, vault, tmp_path, shape):
        import subprocess
        import sys

        sealed = (vault / vs.IDENTITY).read_bytes()
        if shape == "oversized":
            (vault / vs.IDENTITY).write_bytes(sealed + b"\n" * 8192)
        else:
            outside = tmp_path / "outside.age"
            outside.write_bytes(sealed)
            (vault / vs.IDENTITY).unlink()
            (vault / vs.IDENTITY).symlink_to(outside)
        child = subprocess.run(
            [sys.executable, "-I", "-c", svc_mod._UNWRAP, str(vault / vs.IDENTITY)],
            input=PASSPHRASE.encode(),
            capture_output=True,
            timeout=120,
        )
        assert child.returncode == 4 and child.stdout == b""

    async def test_an_oversized_recipient_relocks_without_being_loaded(self, service, vault, db):
        import tracemalloc

        assert (await service.unlock(PASSPHRASE, None))["ok"]
        text = (vault / vs.RECIPIENT).read_text().strip()
        (vault / vs.RECIPIENT).write_text(text + " " * (1 << 20))
        tracemalloc.start()
        try:
            assert (await service.status())["unlocked"] is False
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < (512 << 10)  # a whole-file read would hold 1 MiB at once
        assert (await _events(db))[-1]["event"] == "stale_identity_relock"


class TestReplacementReviewFollowUp:
    async def test_an_oserror_while_checking_is_an_audited_refusal(
        self, service, vault, db, monkeypatch
    ):
        def gone(path=None):
            raise OSError(40, "Too many levels of symbolic links")

        monkeypatch.setattr(vs, "check_ready", gone)
        result = await service.unlock(PASSPHRASE, None)
        assert result == {
            "ok": False,
            "error": "the vault could not be checked (Too many levels of symbolic links)",
        }
        assert (await _events(db))[-1]["event"] == "unlock_refused"

    async def test_status_answers_when_the_vault_directory_cannot_be_read(
        self, service, monkeypatch
    ):
        def denied(path=None):
            raise vs.VaultError("the vault cannot be checked (Permission denied)")

        monkeypatch.setattr(vs, "is_initialised", denied)
        status = await service.status()
        assert status["ok"] and status["initialised"] is False

    def test_a_child_that_dies_at_start_is_not_a_wrong_passphrase(self):
        assert 1 not in svc_mod._UNWRAP_REFUSALS
        assert "wrong passphrase" in svc_mod._UNWRAP_REFUSALS[10]

    def test_the_unwrap_child_refuses_a_fifo(self, vault, tmp_path):
        import subprocess
        import sys

        fifo = tmp_path / "fifo.age"
        os.mkfifo(fifo)
        child = subprocess.run(
            [sys.executable, "-I", "-c", svc_mod._UNWRAP, str(fifo)],
            input=PASSPHRASE.encode(),
            capture_output=True,
            timeout=120,
        )
        assert child.returncode == 4 and child.stdout == b""


class TestFailClosed:
    async def test_no_audit_log_means_no_unlock(self, vault, tmp_path, monkeypatch):
        svc = _make(None, vault, tmp_path)

        async def no_unwrap(passphrase):
            raise AssertionError("no scrypt work without an audit log")

        monkeypatch.setattr(svc, "_unwrap", no_unwrap)
        result = await svc.unlock(PASSPHRASE, None)
        assert not result["ok"] and "audit" in result["error"]
        assert (await svc.status())["audit"] is False

    async def test_a_failed_audit_write_means_no_unlock(self, service, monkeypatch):
        async def broken(db, event, detail=None):
            raise RuntimeError("disk full")

        monkeypatch.setattr(audit, "record_event", broken)
        result = await service.unlock(PASSPHRASE, None)
        assert not result["ok"] and "audit" in result["error"]
        assert not service.unlocked

    async def test_lock_drops_the_key_even_if_its_audit_fails(self, service, monkeypatch):
        assert (await service.unlock(PASSPHRASE, None))["ok"]

        async def broken(db, event, detail=None):
            raise RuntimeError("disk full")

        monkeypatch.setattr(audit, "record_event", broken)
        assert (await service.lock(None))["state"] == "locked"
        assert not service.unlocked

    async def test_a_key_that_does_not_match_recipient_is_not_held(self, service, vault, db):
        (vault / vs.RECIPIENT).write_text(str(x25519.Identity.generate().to_public()) + "\n")
        _rebind(vault)
        result = await service.unlock(PASSPHRASE, None)
        assert result == {"ok": False, "error": "identity.age does not match recipient.txt"}
        assert not service.unlocked
        assert (await _events(db))[-1]["detail"][
            "reason"
        ] == "identity.age does not match recipient.txt"

    @pytest.mark.parametrize("damage", ["garbage", "unreadable"])
    async def test_a_damaged_identity_is_not_called_a_wrong_passphrase(
        self, service, vault, damage
    ):
        if damage == "garbage":
            (vault / vs.IDENTITY).write_bytes(b"not an age file")
        else:
            os.chmod(vault / vs.IDENTITY, 0)
        result = await service.unlock(PASSPHRASE, None)
        os.chmod(vault / vs.IDENTITY, 0o600)
        # Refused before the passphrase is unwrapped, by the store's own checks.
        assert result["ok"] is False and "identity.age" in result["error"]
        assert "wrong passphrase" not in result["error"]

    async def test_an_unreadable_audit_log_is_reported(self, service, monkeypatch):
        async def broken(db, limit=20):
            raise RuntimeError("no such table")

        monkeypatch.setattr(audit, "list_recent", broken)
        assert (await service.status())["audit"] is False

    async def test_uninitialised_vault(self, db, tmp_path):
        svc = _make(db, tmp_path / "none", tmp_path)
        result = await svc.unlock(PASSPHRASE, None)
        assert result["ok"] is False and "not initialised" in result["error"]

    async def test_server_without_pyrage(self, db, vault, tmp_path):
        python = shutil.which("python3", path="/usr/bin")
        if python is None:
            pytest.skip("no system python3")
        svc = _make(db, vault, tmp_path, python=python)
        result = await svc.unlock(PASSPHRASE, None)
        assert not result["ok"] and "payments extra" in result["error"]


async def test_a_failed_start_closes_the_listener(db, vault, tmp_path, monkeypatch):
    svc = _make(db, vault, tmp_path)
    real_lstat = os.lstat

    def failing_lstat(path, *a, **k):
        if str(path) == str(svc._socket_path):
            raise PermissionError("simulated")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(svc_mod.os, "lstat", failing_lstat)
    with pytest.raises(PermissionError):
        await svc.start()
    monkeypatch.setattr(svc_mod.os, "lstat", real_lstat)
    await asyncio.sleep(0)
    assert not svc._server.is_serving()


async def test_start_vault_service_returns_none_when_it_cannot_start(monkeypatch, tmp_path):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    (tmp_path / "run").write_text("a file where the directory should be")
    assert await svc_mod.start_vault_service(type("Rt", (), {"db": None})()) is None


def test_the_unit_pins_core_dumps_off():
    unit = (REPO / "scripts/systemd/genesis-server.service.template").read_text()
    assert "\nLimitCORE=0\n" in unit


class TestRoundOneFindings:
    async def test_a_lock_while_an_unlock_is_queued_wins(self, service, monkeypatch):
        """A lock that lands while an unlock waits behind another one keeps the vault locked."""
        gate = asyncio.Event()
        real = service._unwrap

        async def slow(passphrase):
            await gate.wait()
            return await real(passphrase)

        monkeypatch.setattr(service, "_unwrap", slow)
        first = asyncio.create_task(service.unlock(PASSPHRASE, None))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(service.unlock(PASSPHRASE, None))  # queued behind first
        await asyncio.sleep(0.05)
        await service.lock(None)
        gate.set()
        results = await asyncio.gather(first, second)
        assert not service.unlocked
        assert all(r["ok"] is False for r in results), results

    async def test_a_recipient_changed_during_the_audit_write_is_not_held(
        self, service, vault, monkeypatch
    ):
        real = audit.record_event

        async def swap_recipient(conn, event, detail=None):
            if event == "unlock":
                other = x25519.Identity.generate().to_public()
                (vault / vs.RECIPIENT).write_text(f"{other}\n")
            return await real(conn, event, detail)

        monkeypatch.setattr(audit, "record_event", swap_recipient)
        result = await service.unlock(PASSPHRASE, None)
        assert result == {"ok": False, "error": "recipient.txt changed during the unlock"}
        assert not service.unlocked

    async def test_a_failed_audit_commit_leaves_no_row_behind(self, service, db, monkeypatch):
        async def insert_then_fail(conn, event, detail=None):
            await conn.execute(
                "INSERT INTO payment_vault_events (event, detail, created_at) VALUES (?, ?, ?)",
                (event, None, "2026-01-01T00:00:00+00:00"),
            )
            raise aiosqlite.OperationalError("disk I/O error")  # the COMMIT failed

        monkeypatch.setattr(audit, "record_event", insert_then_fail)
        result = await service.unlock(PASSPHRASE, None)
        assert result["ok"] is False and not service.unlocked
        await db.commit()  # a later, unrelated commit must not persist the stray row
        assert "unlock" not in [e["event"] for e in await _events(db)]

    @pytest.mark.parametrize("name", [vs.IDENTITY, vs.RECIPIENT, ""])
    async def test_a_vault_that_is_not_private_is_not_unlocked(self, service, vault, db, name):
        os.chmod(vault / name, 0o755 if name == "" else 0o644)
        try:
            result = await service.unlock(PASSPHRASE, None)
        finally:
            os.chmod(vault / name, 0o700 if name == "" else 0o600)
        assert result["ok"] is False and "not private" in result["error"]
        assert (await _events(db))[-1]["event"] == "unlock_refused"

    async def test_a_sealed_value_that_is_not_a_key_is_an_audited_refusal(self, service, vault, db):
        from pyrage import passphrase as age_passphrase

        not_a_key = "AGE-SECRET-KEY-1" + "Q" * 58  # the right length, not a key
        (vault / vs.IDENTITY).write_bytes(age_passphrase.encrypt(not_a_key.encode(), PASSPHRASE))
        _rebind(vault)
        result = await service.unlock(PASSPHRASE, None)
        assert result == {"ok": False, "error": "identity.age does not hold an age X25519 key"}
        assert (await _events(db))[-1]["event"] == "unlock_refused"


async def _pending_failed_write(monkeypatch):
    """Patch record_event so an unlock_refused write INSERTs, waits, then fails its COMMIT."""
    real = audit.record_event
    inserted, release = asyncio.Event(), asyncio.Event()

    async def fail_after_insert(conn, event, detail=None):
        if event != "unlock_refused":
            return await real(conn, event, detail)
        await conn.execute(
            "INSERT INTO payment_vault_events (event, detail, created_at) VALUES (?, ?, ?)",
            (event, None, "2026-01-01T00:00:00+00:00"),
        )
        inserted.set()
        await release.wait()
        raise aiosqlite.OperationalError("disk I/O error")  # the COMMIT failed

    monkeypatch.setattr(audit, "record_event", fail_after_insert)
    return inserted, release


class TestRoundOneReview:
    async def test_a_concurrent_audit_write_does_not_commit_a_failed_one(
        self, service, db, monkeypatch
    ):
        inserted, release = await _pending_failed_write(monkeypatch)
        failing = asyncio.create_task(service._audit("unlock_refused", {"reason": "x"}))
        await inserted.wait()
        other = asyncio.create_task(service._audit("lock", None))
        await asyncio.sleep(0.05)
        release.set()
        assert await asyncio.gather(failing, other) == [False, True]
        events = [e["event"] for e in await _events(db)]
        assert "unlock_refused" not in events and events[-1] == "lock"

    async def test_status_never_reports_a_write_that_is_rolled_back(self, service, db, monkeypatch):
        inserted, release = await _pending_failed_write(monkeypatch)
        failing = asyncio.create_task(service._audit("unlock_refused", {"reason": "x"}))
        await inserted.wait()
        status = asyncio.create_task(service.status())
        await asyncio.sleep(0.05)
        release.set()
        await failing
        assert (await status)["last_event"]["event"] == "service_start"

    async def test_a_server_without_pyrage_refuses_the_unlock(self, service, db, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "pyrage", None)  # import pyrage now raises
        result = await service.unlock(PASSPHRASE, None)
        assert result == {"ok": False, "error": svc_mod._UNWRAP_REFUSALS[3]}
        assert (await _events(db))[-1]["event"] == "unlock_refused"

    async def test_an_unwrapped_value_that_is_not_text_is_unreadable_not_a_wrong_passphrase(
        self, service, vault
    ):
        from pyrage import passphrase as age_passphrase

        not_text = b"\xff" * 74  # the length of a key, so the sealed file keeps its shape
        (vault / vs.IDENTITY).write_bytes(age_passphrase.encrypt(not_text, PASSPHRASE))
        _rebind(vault)
        result = await service.unlock(PASSPHRASE, None)
        assert result == {"ok": False, "error": svc_mod._UNWRAP_REFUSALS[4]}


class TestRoundOneReviewFollowUp:
    async def test_start_owns_its_audit_connection_and_closes_it_at_stop(
        self, vault, tmp_path, monkeypatch
    ):
        from genesis.env import genesis_db_path

        dbp = genesis_db_path()  # conftest pins it to a per-test temp file
        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "h"))
        monkeypatch.setattr(vs, "vault_dir", lambda: vault)
        async with aiosqlite.connect(str(dbp)) as conn:
            await MIG.up(conn)
        shared = object()
        svc = await svc_mod.start_vault_service(type("Rt", (), {"db": shared})())
        assert svc is not None and svc.owned_db is not None
        assert svc._db is svc.owned_db and svc._db is not shared
        assert (await svc.unlock(PASSPHRASE, None))["ok"]
        await svc_mod.stop_vault_service(svc)
        with pytest.raises(ValueError):
            await svc.owned_db.execute("SELECT 1")

    async def test_a_connection_that_will_not_open_leaves_unlock_refused(
        self, vault, tmp_path, monkeypatch
    ):
        import genesis.db.connection as connection

        async def no_db(path):
            raise OSError("no database")

        monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "h"))
        monkeypatch.setattr(connection, "get_db", no_db)
        monkeypatch.setattr(vs, "vault_dir", lambda: vault)
        svc = await svc_mod.start_vault_service(type("Rt", (), {"db": None})())
        try:
            assert svc is not None
            result = await svc.unlock(PASSPHRASE, None)
            assert not result["ok"] and "audit log is unavailable" in result["error"]
        finally:
            await svc_mod.stop_vault_service(svc)

    async def test_an_unlocked_vault_answers_already_unlocked_not_a_refusal(
        self, service, vault, db
    ):
        assert (await service.unlock(PASSPHRASE, None))["ok"]
        os.chmod(vault / vs.IDENTITY, 0o644)  # would fail check_ready now
        try:
            result = await service.unlock(PASSPHRASE, None)
        finally:
            os.chmod(vault / vs.IDENTITY, 0o600)
        assert result == {"ok": True, "state": "already unlocked"}
        assert (await _events(db))[-1]["event"] == "unlock"

    async def test_an_unwrap_helper_that_cannot_start_is_an_audited_refusal(
        self, db, vault, tmp_path
    ):
        svc = _make(db, vault, tmp_path, python=str(tmp_path / "no-such-python"))
        result = await svc.unlock(PASSPHRASE, None)
        assert result == {"ok": False, "error": svc_mod._UNWRAP_REFUSALS[127]}
        assert (await _events(db))[-1]["event"] == "unlock_refused"

    async def test_stop_is_bounded_behind_a_stuck_audit_write(self, service, monkeypatch):
        async def stuck(conn, event, detail=None):
            await asyncio.sleep(3600)

        monkeypatch.setattr(svc_mod, "_STOP_WAIT_S", 0.2)
        monkeypatch.setattr(audit, "record_event", stuck)
        started = time.monotonic()
        await asyncio.wait_for(service.stop(), 10)
        assert time.monotonic() - started < 5


async def test_a_key_file_altered_since_init_is_refused_before_any_unwrap(
    service, vault, db, monkeypatch
):
    """Without a rebind, vault_store's recorded key pair refuses first, and no scrypt runs."""

    async def no_unwrap(passphrase):
        raise AssertionError("refused before the unwrap")

    monkeypatch.setattr(service, "_unwrap", no_unwrap)
    (vault / vs.RECIPIENT).write_text(str(x25519.Identity.generate().to_public()) + "\n")
    result = await service.unlock(PASSPHRASE, None)
    assert result["ok"] is False and "recipient.txt is not the public key" in result["error"]
    assert (await _events(db))[-1]["event"] == "unlock_refused"
