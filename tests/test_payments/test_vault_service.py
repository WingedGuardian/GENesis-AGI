"""Tests for the vault service: the key read from identity.key, the Unix socket, the audit trail."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from pyrage import passphrase as age_passphrase
from pyrage import x25519

from genesis.db.crud import payment_vault_events as audit
from genesis.payments import vault_migrate as vm
from genesis.payments import vault_release_store as vrs
from genesis.payments import vault_service as svc_mod
from genesis.payments import vault_store as vs
from genesis.payments.peer_check import FOREGROUND, Peer, PeerRefused, start_of
from genesis.payments.vault_service import VaultService

PW = "quartz lantern ember noodle 41"  # only the legacy (sealed) vaults have one
MIG = importlib.import_module("genesis.db.migrations.20261011060631_payment_vault_events")
REPO = Path(__file__).resolve().parents[2]
VISA = "4242" * 4  # Stripe's public test Visa
CLAIM_HASH = hashlib.sha256(b"claim-secret-for-tests").hexdigest()


@pytest.fixture(scope="module")
def _template(tmp_path_factory):
    path = tmp_path_factory.mktemp("template") / "payment_vault"
    vs.init_vault(path)
    return path


@pytest.fixture
def vault(_template, tmp_path):
    path = tmp_path / "payment_vault"
    shutil.copytree(_template, path)
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

    vault_store refuses a key file that is not the pair init recorded, before
    any later check. Tests of the checks behind it make that one pass first.
    """
    digest = hashlib.sha256((vault / vs.IDENTITY).read_bytes()).hexdigest()
    recipient = (vault / vs.RECIPIENT).read_text().strip()
    with sqlite3.connect(vault / vs.CARDS) as conn:
        conn.executemany(
            "UPDATE vault_meta SET value = ? WHERE key = ?",
            [(digest, "identity_sha256"), (recipient, "recipient")],
        )


def _make_legacy(vault: Path) -> None:
    """Turn a new vault into one made before the passphrase was dropped (identity.age only)."""
    key = (vault / vs.IDENTITY).read_bytes()
    sealed = age_passphrase.encrypt(key.strip(), PW)
    fd = os.open(vault / vs.SEALED_IDENTITY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, sealed)
    finally:
        os.close(fd)
    (vault / vs.IDENTITY).unlink()
    with sqlite3.connect(vault / vs.CARDS) as conn:
        conn.execute(
            "UPDATE vault_meta SET value = ? WHERE key = 'identity_sha256'",
            (hashlib.sha256(sealed).hexdigest(),),
        )


def _add_card(vault: Path) -> str:
    card = vs.parse_card(VISA, f"12/{datetime.now(UTC).year + 3}", "987")
    return vs.add_card(
        card, label="Test card", merchant="example.com", single_use=False, path=vault
    )


async def _events(db) -> list[dict]:
    return list(reversed(await audit.list_recent(db, 100)))


async def _names(db) -> list[str]:
    return [e["event"] for e in await _events(db)]


def _replace_recipient(vault: Path) -> None:
    (vault / vs.RECIPIENT).write_text(str(x25519.Identity.generate().to_public()) + "\n")


async def _until(pred, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.005)


class FakeRegistry:
    """PaymentApprovalRegistry's contract: request(fields) awaits a future the test holds."""

    def __init__(self) -> None:
        self.futures: list[asyncio.Future] = []

    def attach_audit(self, _audit) -> None:
        pass

    async def request(self, fields, *, audit_detail=None, timeout_s=None) -> str:
        fut = asyncio.get_running_loop().create_future()
        self.futures.append(fut)
        return await fut

    async def notify(self, text: str) -> bool:
        return True


def _me() -> Peer:
    pid = os.getpid()
    return Peer(
        pid=pid,
        start=start_of(pid),
        comm="python",
        chain=("terse", "claude"),
        claude_pid=4321,
        kind=FOREGROUND,
    )


def _release_service(db, vault, tmp_path) -> tuple[VaultService, FakeRegistry]:
    reg = FakeRegistry()
    svc = _make(db, vault, tmp_path, approvals=reg, release_peer=lambda pid, **_k: _me())
    return svc, reg


def _release_request(card_ref: str) -> dict:
    return {
        "op": "release_request",
        "card_ref": card_ref,
        "host": "shop.example.com",
        "page_title": "Checkout",
        "amount": "12.00 USD",
        "claim_hash": CLAIM_HASH,
        "field_origins": {"number": "https://js.stripe.com", "cvc": "https://js.stripe.com"},
        "button_label": "Pay 12.00 USD",
        "also_filled": ["name"],
    }


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
            b'{"op": 7}\n',
        ],
    )
    async def test_bad_requests_get_a_fixed_answer(self, service, raw):
        reply = await _ask(service, None, raw=raw)
        assert reply == {"ok": False, "error": "bad request"}

    @pytest.mark.parametrize(
        "payload",
        [{"op": "unlock"}, {"op": "unlock", "passphrase": PW}, {"op": "lock"}],
    )
    async def test_the_retired_unlock_and_lock_ops_answer_why(self, service, db, payload):
        before = await _names(db)
        assert await _ask(service, payload) == {"ok": False, "error": vm.RETIRED}
        assert service.unlocked  # a lock no longer drops the key
        assert await _names(db) == before  # and writes nothing

    async def test_the_peer_is_recorded(self, db, vault, tmp_path):
        def refuse(pid, **_k):
            raise PeerRefused("not the MCP server", {"why": "test"})

        svc = _make(db, vault, tmp_path, release_peer=refuse)
        await svc.start()
        try:
            reply = await _ask(svc, {"op": "release_status", "nonce": "x"})
        finally:
            await svc.stop()
        assert reply == {"ok": False, "error": "peer not allowed"}
        refused = next(e for e in await _events(db) if e["event"] == "release_refused")
        peer = refused["detail"]["peer"]
        assert peer["pid"] == os.getpid() and peer["uid"] == os.getuid()
        assert peer["comm"] and peer["parent"] and isinstance(peer["start"], int)
        assert peer["why"] == "test"


class TestKeyLoading:
    async def test_start_loads_the_key_and_records_it_before_holding_it(
        self, db, vault, tmp_path, monkeypatch
    ):
        svc = _make(db, vault, tmp_path)
        real = audit.record_event
        held_while_written = []

        async def spy(conn, event, detail=None):
            if event == "key_loaded":
                held_while_written.append(svc.unlocked)
            return await real(conn, event, detail)

        monkeypatch.setattr(audit, "record_event", spy)
        await svc.start()
        try:
            assert held_while_written == [False]
            assert svc.unlocked and svc._loaded_at is not None
            expected = str(vrs.load_identity(vault).to_public())
            assert str(svc._identity.to_public()) == expected
            assert await _names(db) == ["service_start", "key_loaded"]
            status = await svc.status()
            assert status["unlocked"] and status["loaded_at"] == svc._loaded_at
            assert status["ready_error"] is None and status["initialised"]
            assert status["last_event"]["event"] == "key_loaded"
            assert set(status) == {
                "ok", "unlocked", "loaded_at", "initialised", "ready_error",
                "payments_extra", "audit", "last_event",
            }  # fmt: skip
        finally:
            await svc.stop()

    async def test_a_failed_key_loaded_row_means_no_key(self, db, vault, tmp_path, monkeypatch):
        real = audit.record_event

        async def lose_it(conn, event, detail=None):
            if event == "key_loaded":
                raise sqlite3.OperationalError("disk full")
            return await real(conn, event, detail)

        monkeypatch.setattr(audit, "record_event", lose_it)
        svc = _make(db, vault, tmp_path)
        await svc.start()
        try:
            assert not svc.unlocked and svc._loaded_at is None
            assert "key_loaded" not in await _names(db)
            status = await svc.status()  # tries again, and the row fails again
            assert status["unlocked"] is False and status["ready_error"]
        finally:
            await svc.stop()

    async def test_a_refused_load_is_recorded_once_per_reason(self, db, vault, tmp_path):
        os.chmod(vault / vs.IDENTITY, 0o644)
        svc = _make(db, vault, tmp_path)
        await svc.start()
        try:
            for _ in range(3):  # status polls
                status = await svc.status()
                assert not status["unlocked"] and "not private" in status["ready_error"]
            assert await _names(db) == ["service_start", "key_load_refused"]
            first = (await _events(db))[-1]["detail"]["reason"]
            assert "not private" in first and status["ready_error"] == first
            os.chmod(vault / vs.IDENTITY, 0o600)
            os.chmod(vault / vs.RECIPIENT, 0o644)  # a different reason
            await svc.status()
            await svc.status()
            assert await _names(db) == ["service_start", "key_load_refused", "key_load_refused"]
            assert "recipient.txt" in (await _events(db))[-1]["detail"]["reason"]
            os.chmod(vault / vs.RECIPIENT, 0o600)
            status = await svc.status()  # fixed: loaded at the next status
            assert status["unlocked"] and status["ready_error"] is None
            assert (await _names(db))[-1] == "key_loaded"
        finally:
            await svc.stop()

    async def test_status_reports_readiness_from_the_last_refusal(self, service, db, vault):
        service._identity = None  # as if never loaded
        os.chmod(vault / vs.IDENTITY, 0o644)
        status = await service.status()
        assert status["initialised"] and "not private" in status["ready_error"]
        os.chmod(vault / vs.IDENTITY, 0o600)
        assert (await service.status())["ready_error"] is None

    async def test_concurrent_loads_load_once(self, db, vault, tmp_path):
        svc = _make(db, vault, tmp_path)
        await asyncio.gather(*(svc.ensure_key() for _ in range(5)))
        assert svc.unlocked
        assert await _names(db) == ["key_loaded"]

    async def test_the_load_keeps_the_event_loop_responsive(self, db, vault, tmp_path, monkeypatch):
        real = vrs.load_identity

        def slow(path=None):
            time.sleep(0.3)
            return real(path)

        monkeypatch.setattr(vrs, "load_identity", slow)
        svc = _make(db, vault, tmp_path)
        lag, done = 0.0, asyncio.Event()

        async def ticker():
            nonlocal lag
            while not done.is_set():
                t = time.perf_counter()
                await asyncio.sleep(0.01)
                lag = max(lag, time.perf_counter() - t - 0.01)

        tick = asyncio.create_task(ticker())
        await svc.ensure_key()
        done.set()
        await tick
        assert svc.unlocked
        assert lag < 0.15, f"loop stalled {lag:.2f}s during the load"

    async def test_no_reply_or_event_holds_the_key(self, service, db, vault):
        key = (vault / vs.IDENTITY).read_text().strip()
        replies = [await _ask(service, {"op": "status"}), await service.status()]
        await service.stop()
        text = json.dumps(replies) + json.dumps(await _events(db))
        assert key not in text and "AGE-SECRET-KEY" not in text.upper()


class TestLegacyAndMigration:
    async def test_a_legacy_vault_holds_no_key_and_says_to_migrate(self, db, vault, tmp_path):
        _make_legacy(vault)
        svc = _make(db, vault, tmp_path)
        await svc.start()
        try:
            status = await svc.status()
            await svc.status()
            assert status["unlocked"] is False and status["initialised"] is True
            assert status["ready_error"] == vm._NEEDS_MIGRATE
            assert "vault migrate" in status["ready_error"]
            assert await _names(db) == ["service_start", "key_load_refused"]
            assert (await _events(db))[-1]["detail"]["reason"] == vm._NEEDS_MIGRATE
        finally:
            await svc.stop()

    async def test_a_vault_migrated_under_the_service_is_loaded_at_the_next_status(
        self, db, vault, tmp_path
    ):
        _make_legacy(vault)
        svc = _make(db, vault, tmp_path)
        await svc.start()
        try:
            assert not svc.unlocked
            await asyncio.to_thread(vm.migrate_vault, PW, vault)
            status = await svc.status()
            assert status["unlocked"] is True and status["ready_error"] is None
            assert await _names(db) == ["service_start", "key_load_refused", "key_loaded"]
        finally:
            await svc.stop()

    async def test_a_vault_migrated_under_the_service_is_loaded_at_the_next_release_request(
        self, db, vault, tmp_path
    ):
        ref = _add_card(vault)
        _make_legacy(vault)
        svc, reg = _release_service(db, vault, tmp_path)
        await svc.start()
        try:
            assert not svc.unlocked
            await asyncio.to_thread(vm.migrate_vault, PW, vault)
            reply = await _ask(svc, _release_request(ref))
            assert reply["ok"], reply
            assert svc.unlocked
            names = await _names(db)
            assert names[:3] == ["service_start", "key_load_refused", "key_loaded"]
            assert "release_requested" in names[3:]
        finally:
            await svc.stop()


class TestStaleKey:
    async def test_a_stale_key_is_dropped_and_a_pending_release_refused(self, db, vault, tmp_path):
        ref = _add_card(vault)
        svc, reg = _release_service(db, vault, tmp_path)
        await svc.start()
        try:
            nonce = (await _ask(svc, _release_request(ref)))["nonce"]
            await _until(lambda: reg.futures)
            epoch = svc._epoch
            _replace_recipient(vault)
            status = await svc.status()
            assert status["unlocked"] is False and svc._identity is None
            assert status["loaded_at"] is None
            assert svc._epoch == epoch + 1
            events = await _events(db)
            dropped = next(e for e in events if e["event"] == "key_dropped")
            assert dropped["detail"] == {"reason": "the held key no longer matches recipient.txt"}
            refused = [e for e in events if e["event"] == "release_refused"]
            assert refused and refused[-1]["detail"]["reason"] == dropped["detail"]["reason"]
            # Then the new recipient.txt is refused on its own terms.
            assert events[-1]["event"] == "key_load_refused"
            assert "recipient.txt" in status["ready_error"]
            assert (await _ask(svc, {"op": "release_status", "nonce": nonce}))["state"] == "refused"
            # The tap's wait is retired with it: no later grant can land.
            await _until(lambda: reg.futures[0].cancelled())
        finally:
            await svc.stop()

    async def test_an_oversized_recipient_drops_the_key_without_being_loaded(
        self, service, vault, db
    ):
        import tracemalloc

        assert service.unlocked
        text = (vault / vs.RECIPIENT).read_text().strip()
        (vault / vs.RECIPIENT).write_text(text + " " * (1 << 20))
        tracemalloc.start()
        try:
            assert (await service.status())["unlocked"] is False
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < (512 << 10)  # a whole-file read would hold 1 MiB at once
        assert "key_dropped" in await _names(db)

    async def test_a_stale_key_is_dropped_even_if_its_row_fails(self, service, vault, monkeypatch):
        async def broken(db, event, detail=None):
            raise RuntimeError("disk full")

        monkeypatch.setattr(audit, "record_event", broken)
        _replace_recipient(vault)
        await service.status()
        assert not service.unlocked

    async def test_a_recipient_changed_during_the_key_loaded_row_is_not_held(
        self, db, vault, tmp_path, monkeypatch
    ):
        real = audit.record_event

        async def swap_recipient(conn, event, detail=None):
            if event == "key_loaded":
                _replace_recipient(vault)
            return await real(conn, event, detail)

        monkeypatch.setattr(audit, "record_event", swap_recipient)
        svc = _make(db, vault, tmp_path)
        await svc.ensure_key()
        assert not svc.unlocked
        events = await _events(db)
        assert [e["event"] for e in events] == ["key_loaded", "key_dropped"]
        assert events[-1]["detail"] == {"reason": "the vault changed during the load"}


class TestStop:
    async def test_stop_drops_the_key_and_records_had_key(self, service, db):
        assert service.unlocked
        await service.stop()
        assert not service.unlocked and service._loaded_at is None
        last = (await _events(db))[-1]
        assert last["event"] == "service_stop" and last["detail"] == {"had_key": True}

    async def test_stop_with_no_key_records_had_key_false(self, db, vault, tmp_path):
        _make_legacy(vault)
        svc = _make(db, vault, tmp_path)
        await svc.start()
        await svc.stop()
        last = (await _events(db))[-1]
        assert last["event"] == "service_stop" and last["detail"] == {"had_key": False}

    async def test_stop_drops_the_key_even_if_its_row_fails(self, service, monkeypatch):
        async def broken(db, event, detail=None):
            raise RuntimeError("disk full")

        monkeypatch.setattr(audit, "record_event", broken)
        await service.stop()
        assert not service.unlocked

    async def test_a_stop_during_the_load_wins(self, db, vault, tmp_path, monkeypatch):
        real = vrs.load_identity
        entered, release = threading.Event(), threading.Event()

        def held(path=None):
            entered.set()
            assert release.wait(5)
            return real(path)

        monkeypatch.setattr(vrs, "load_identity", held)
        svc = _make(db, vault, tmp_path)
        loading = asyncio.create_task(svc.ensure_key())
        await asyncio.to_thread(entered.wait, 5)
        await svc.stop()
        release.set()
        await loading
        assert not svc.unlocked
        assert await _names(db) == ["service_stop"]

    async def test_a_stop_during_the_key_loaded_row_wins(self, db, vault, tmp_path, monkeypatch):
        real = audit.record_event
        svc = _make(db, vault, tmp_path)
        racers: list[asyncio.Task] = []

        async def racing(conn, event, detail=None):
            if event == "key_loaded":
                # A separate task, as in production: it takes effect at once and
                # its own row queues behind this write.
                racers.append(asyncio.create_task(svc.stop()))
                await asyncio.sleep(0)
            await real(conn, event, detail)

        monkeypatch.setattr(audit, "record_event", racing)
        await asyncio.wait_for(svc.ensure_key(), 30)
        await asyncio.wait_for(asyncio.gather(*racers), 30)
        assert not svc.unlocked and svc._loaded_at is None
        events = await _events(db)
        assert [e["event"] for e in events] == ["key_loaded", "service_stop"]
        assert events[-1]["detail"] == {"had_key": False}

    async def test_stop_is_bounded_behind_a_stuck_audit_write(self, service, monkeypatch):
        async def stuck(conn, event, detail=None):
            await asyncio.sleep(3600)

        monkeypatch.setattr(svc_mod, "_STOP_WAIT_S", 0.2)
        monkeypatch.setattr(audit, "record_event", stuck)
        started = time.monotonic()
        await asyncio.wait_for(service.stop(), 10)
        assert time.monotonic() - started < 5


class TestFailClosed:
    async def test_no_audit_log_means_no_key(self, vault, tmp_path, monkeypatch):
        svc = _make(None, vault, tmp_path)

        def no_load(path=None):
            raise AssertionError("no load without an audit log")

        monkeypatch.setattr(vrs, "load_identity", no_load)
        status = await svc.status()
        assert status["audit"] is False and status["unlocked"] is False
        assert status["ready_error"] == "the key is not loaded"

    async def test_a_failed_audit_commit_leaves_no_row_behind(
        self, db, vault, tmp_path, monkeypatch
    ):
        async def insert_then_fail(conn, event, detail=None):
            await conn.execute(
                "INSERT INTO payment_vault_events (event, detail, created_at) VALUES (?, ?, ?)",
                (event, None, "2026-01-01T00:00:00+00:00"),
            )
            raise aiosqlite.OperationalError("disk I/O error")  # the COMMIT failed

        monkeypatch.setattr(audit, "record_event", insert_then_fail)
        svc = _make(db, vault, tmp_path)
        await svc.ensure_key()
        assert not svc.unlocked
        await db.commit()  # a later, unrelated commit must not persist the stray row
        assert "key_loaded" not in await _names(db)

    async def test_an_oserror_while_loading_is_a_recorded_refusal(
        self, db, vault, tmp_path, monkeypatch
    ):
        def gone(path=None):
            raise OSError(40, "Too many levels of symbolic links")

        monkeypatch.setattr(vrs, "load_identity", gone)
        svc = _make(db, vault, tmp_path)
        status = await svc.status()
        reason = "the vault could not be read (Too many levels of symbolic links)"
        assert status["ready_error"] == reason
        last = (await _events(db))[-1]
        assert last["event"] == "key_load_refused" and last["detail"] == {"reason": reason}

    async def test_status_answers_when_the_vault_directory_cannot_be_read(
        self, service, monkeypatch
    ):
        def denied(path=None):
            raise vs.VaultError("the vault cannot be checked (Permission denied)")

        monkeypatch.setattr(vs, "is_initialised", denied)
        status = await service.status()
        assert status["ok"] and status["initialised"] is False

    async def test_a_key_that_does_not_match_recipient_is_not_held(self, db, vault, tmp_path):
        _replace_recipient(vault)
        _rebind(vault)
        svc = _make(db, vault, tmp_path)
        await svc.ensure_key()
        assert not svc.unlocked
        reason = (await _events(db))[-1]["detail"]["reason"]
        assert (
            reason == "identity.key and recipient.txt do not match; restore the vault from backup"
        )

    async def test_a_key_file_altered_since_init_is_refused(self, db, vault, tmp_path):
        """Without a rebind, the key pair cards.db records refuses first."""
        _replace_recipient(vault)
        svc = _make(db, vault, tmp_path)
        await svc.ensure_key()
        assert not svc.unlocked
        last = (await _events(db))[-1]
        assert last["event"] == "key_load_refused"
        assert "recipient.txt is not the public key" in last["detail"]["reason"]

    @pytest.mark.parametrize(
        "content",
        [b"AGE-SECRET-KEY-1" + b"Q" * 58 + b"\n", b"\xff" * 74 + b"\n", b"not a key"],
        ids=["bad-checksum", "not-text", "garbage"],
    )
    async def test_a_damaged_key_file_is_a_recorded_refusal(self, db, vault, tmp_path, content):
        (vault / vs.IDENTITY).write_bytes(content)
        _rebind(vault)
        svc = _make(db, vault, tmp_path)
        await svc.ensure_key()
        assert not svc.unlocked
        last = (await _events(db))[-1]
        assert last["event"] == "key_load_refused"
        assert "identity.key is not a whole age secret key" in last["detail"]["reason"]

    async def test_an_unreadable_key_file_is_a_recorded_refusal(self, db, vault, tmp_path):
        os.chmod(vault / vs.IDENTITY, 0)
        try:
            svc = _make(db, vault, tmp_path)
            await svc.ensure_key()
        finally:
            os.chmod(vault / vs.IDENTITY, 0o600)
        assert not svc.unlocked
        last = (await _events(db))[-1]
        assert last["event"] == "key_load_refused" and "identity.key" in last["detail"]["reason"]

    @pytest.mark.parametrize("name", [vs.IDENTITY, vs.RECIPIENT, ""])
    async def test_a_vault_that_is_not_private_is_not_loaded(self, db, vault, tmp_path, name):
        os.chmod(vault / name, 0o755 if name == "" else 0o644)
        try:
            svc = _make(db, vault, tmp_path)
            await svc.ensure_key()
        finally:
            os.chmod(vault / name, 0o700 if name == "" else 0o600)
        assert not svc.unlocked
        last = (await _events(db))[-1]
        assert last["event"] == "key_load_refused" and "not private" in last["detail"]["reason"]

    async def test_an_unreadable_audit_log_is_reported(self, service, monkeypatch):
        async def broken(db, limit=20, **_kw):
            raise RuntimeError("no such table")

        monkeypatch.setattr(audit, "list_recent", broken)
        assert (await service.status())["audit"] is False

    async def test_uninitialised_vault(self, db, tmp_path):
        svc = _make(db, tmp_path / "none", tmp_path)
        status = await svc.status()
        assert status["unlocked"] is False and status["initialised"] is False
        assert status["ready_error"] is None
        last = (await _events(db))[-1]
        assert last["event"] == "key_load_refused" and "not initialised" in last["detail"]["reason"]

    async def test_a_server_without_pyrage_holds_no_key(self, db, vault, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "pyrage", None)  # import pyrage now raises
        svc = _make(db, vault, tmp_path)
        await svc.ensure_key()
        assert not svc.unlocked
        last = (await _events(db))[-1]
        assert last["event"] == "key_load_refused"
        assert last["detail"]["reason"] == svc_mod._NO_EXTRA

    async def test_a_held_key_is_not_rechecked_beyond_its_recipient(self, service, vault, db):
        before = await _names(db)
        os.chmod(vault / vs.IDENTITY, 0o644)  # would refuse a load now
        try:
            status = await service.status()
        finally:
            os.chmod(vault / vs.IDENTITY, 0o600)
        assert status["unlocked"] is True
        assert await _names(db) == before


def test_the_default_clock_counts_suspended_time(db, vault, tmp_path):
    svc = _make(db, vault, tmp_path)
    assert abs(svc._clock() - time.clock_gettime(time.CLOCK_BOOTTIME)) < 1.0
    assert svc._clock.__code__.co_names.count("CLOCK_BOOTTIME") == 1


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


class TestStartVaultService:
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
        try:  # a failed assertion must still close the connection, or the run hangs
            assert svc is not None and svc.owned_db is not None
            assert svc._db is svc.owned_db and svc._db is not shared
            assert svc.unlocked  # loaded at start
        finally:
            await svc_mod.stop_vault_service(svc)
        with pytest.raises(ValueError):
            await svc.owned_db.execute("SELECT 1")

    async def test_a_connection_that_will_not_open_leaves_no_key(
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
            assert svc is not None and not svc.unlocked
            status = await svc.status()
            assert status["audit"] is False and status["unlocked"] is False
        finally:
            await svc_mod.stop_vault_service(svc)


async def _pending_failed_write(monkeypatch, target: str = "key_load_refused"):
    """Patch record_event so a ``target`` write INSERTs, waits, then fails its COMMIT."""
    real = audit.record_event
    inserted, release = asyncio.Event(), asyncio.Event()

    async def fail_after_insert(conn, event, detail=None):
        if event != target:
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


class TestAuditWrites:
    async def test_a_concurrent_audit_write_does_not_commit_a_failed_one(
        self, service, db, monkeypatch
    ):
        inserted, release = await _pending_failed_write(monkeypatch)
        failing = asyncio.create_task(service._audit("key_load_refused", {"reason": "x"}))
        await inserted.wait()
        other = asyncio.create_task(service._audit("key_dropped", None))
        await asyncio.sleep(0.05)
        release.set()
        assert await asyncio.gather(failing, other) == [False, True]
        events = await _names(db)
        assert "key_load_refused" not in events and events[-1] == "key_dropped"

    async def test_status_never_reports_a_write_that_is_rolled_back(self, service, db, monkeypatch):
        inserted, release = await _pending_failed_write(monkeypatch)
        failing = asyncio.create_task(service._audit("key_load_refused", {"reason": "x"}))
        await inserted.wait()
        status = asyncio.create_task(service.status())
        await asyncio.sleep(0.05)
        release.set()
        await failing
        assert (await status)["last_event"]["event"] == "key_loaded"


class TestAuditCancellation:
    @pytest.mark.parametrize("wrapped", [False, True])
    async def test_cancelled_insert_stays_owned_until_sql_and_rollback_finish(
        self,
        db,
        vault,
        tmp_path,
        monkeypatch,
        wrapped,
    ):
        from genesis.db.connection import SerializedConnection

        svc = _make(SerializedConnection(db) if wrapped else db, vault, tmp_path)
        svc._identity = vrs.load_identity(vault)  # held already: status writes no load row
        entered, rolling = asyncio.Event(), asyncio.Event()
        release_sql, release_rollback = threading.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        rollback = db.rollback

        def hold_insert():
            loop.call_soon_threadsafe(entered.set)
            assert release_sql.wait(5)
            return 0

        async def hold_rollback():
            rolling.set()
            await release_rollback.wait()
            await rollback()

        await db.create_function("hold_insert", 0, hold_insert)
        await db.execute(
            "CREATE TEMP TRIGGER hold BEFORE INSERT ON payment_vault_events "
            "WHEN NEW.event = 'key_loaded' BEGIN SELECT hold_insert(); END"
        )
        monkeypatch.setattr(db, "rollback", hold_rollback)
        caller = asyncio.create_task(svc._audit("key_loaded"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
            await asyncio.wait_for(rolling.wait(), 2)
            assert svc._audit_lock.locked()
            async with aiosqlite.connect(str(tmp_path / "g.db"), timeout=0.01) as other:
                with pytest.raises(aiosqlite.OperationalError, match="locked"):
                    await audit.record_event(other, "peer_refused")
            status = asyncio.create_task(svc.status())
            writer = asyncio.create_task(svc._audit("key_dropped"))
            for _ in range(3):
                await asyncio.sleep(0)
            assert not status.done() and not writer.done()
            # Repeated cancellation of the departed caller cannot touch cleanup.
            caller.cancel()
            release_sql.set()
            release_rollback.set()
            assert await asyncio.wait_for(writer, 3)
            result = await status
            assert result["last_event"] is None or result["last_event"]["event"] != "key_loaded"
            assert await _names(db) == ["key_dropped"]
            async with aiosqlite.connect(str(tmp_path / "g.db"), timeout=0.1) as other:
                await audit.record_event(other, "peer_refused")
            await asyncio.sleep(0)
            assert not svc._audit_owners
        finally:
            release_sql.set()
            release_rollback.set()
            await svc.stop()

    async def test_cancellation_while_waiting_for_lock_cannot_rollback_owner(
        self,
        service,
        db,
        monkeypatch,
    ):
        committed, release = asyncio.Event(), asyncio.Event()
        commit = db.commit
        rollback = db.rollback
        rollbacks = 0

        async def hold_commit():
            committed.set()
            await release.wait()
            await commit()

        async def count_rollback():
            nonlocal rollbacks
            rollbacks += 1
            await rollback()

        monkeypatch.setattr(db, "commit", hold_commit)
        monkeypatch.setattr(db, "rollback", count_rollback)
        owner = asyncio.create_task(service._audit("key_dropped"))
        await committed.wait()
        waiter = asyncio.create_task(service._audit("key_load_refused"))
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await asyncio.sleep(0)
            assert rollbacks == 0 and db.in_transaction
        finally:
            release.set()
            await owner
        assert (await _events(db))[-1]["event"] == "key_dropped"

    @pytest.mark.parametrize("cancel_first", [False, True])
    async def test_rollback_is_protected_from_cancellation_and_stop(
        self,
        service,
        db,
        monkeypatch,
        cancel_first,
    ):
        inserted, release = await _pending_failed_write(monkeypatch)
        rolling, finish = asyncio.Event(), asyncio.Event()
        rollback = db.rollback

        async def hold_rollback():
            rolling.set()
            await finish.wait()
            await rollback()

        monkeypatch.setattr(db, "rollback", hold_rollback)
        monkeypatch.setattr(svc_mod, "_STOP_WAIT_S", 0.01)
        caller = asyncio.create_task(service._audit("key_load_refused"))
        await inserted.wait()
        if cancel_first:
            caller.cancel()
        else:
            release.set()
        try:
            await rolling.wait()
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
            await asyncio.wait_for(service.stop(), 1)
            assert service._audit_lock.locked() and service._audit_owners
            # Even directly repeated owner cancellation cannot release its lock.
            for owner in service._audit_owners:
                owner.cancel()
            await asyncio.sleep(0)
            assert service._audit_lock.locked()
        finally:
            finish.set()
            release.set()
            if service._audit_owners:
                await asyncio.wait(service._audit_owners, timeout=2)
        assert not service._audit_owners and not db.in_transaction
        assert "key_load_refused" not in await _names(db)

    async def test_rollback_failure_retires_connection_and_shares_protected_close(
        self,
        service,
        db,
        monkeypatch,
    ):
        inserted, release = await _pending_failed_write(monkeypatch)
        closing, finish = asyncio.Event(), asyncio.Event()
        close = db.close
        closes = 0

        async def fail_rollback():
            raise aiosqlite.OperationalError("rollback failed")

        async def hold_close():
            nonlocal closes
            closes += 1
            closing.set()
            await finish.wait()
            await close()

        monkeypatch.setattr(db, "rollback", fail_rollback)
        monkeypatch.setattr(db, "close", hold_close)
        service.owned_db = db
        caller = asyncio.create_task(service._audit("key_load_refused"))
        await inserted.wait()
        status = asyncio.create_task(service.status())
        writer = asyncio.create_task(service._audit("key_dropped"))
        release.set()
        try:
            assert not await caller
            await closing.wait()
            assert service._db is None
            assert not (await status)["audit"] and not await writer
            service._identity = None  # with the audit log gone, no key is loaded again
            await service.ensure_key()
            assert not service.unlocked
            stopping = asyncio.create_task(svc_mod.stop_vault_service(service))
            for _ in range(3):
                await asyncio.sleep(0)
            stopping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert closes == 1 and not service._audit_close.cancelled()
        finally:
            finish.set()
            await svc_mod.stop_vault_service(service)
        assert closes == 1

    async def test_queued_commit_is_not_promised_to_be_undone(self, service, db, monkeypatch):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        commit = db.commit

        def hold_commit():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return 0

        async def queued_commit():
            # One real SQLite worker item: cancellation cannot retract COMMIT.
            def run():
                hold_commit()
                db._conn.commit()

            await db._execute(run)

        monkeypatch.setattr(db, "commit", queued_commit)
        caller = asyncio.create_task(service._audit("key_dropped"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
        finally:
            release.set()
            monkeypatch.setattr(db, "commit", commit)
            if service._audit_owners:
                await asyncio.wait(service._audit_owners, timeout=2)
        assert not db.in_transaction
        assert (await _events(db))[-1]["event"] == "key_dropped"

    async def test_a_cancelled_load_never_holds_the_key(self, db, vault, tmp_path, monkeypatch):
        inserted, release = asyncio.Event(), asyncio.Event()
        commit = db.commit

        async def hold_commit():
            inserted.set()
            await release.wait()
            await commit()

        monkeypatch.setattr(db, "commit", hold_commit)
        svc = _make(db, vault, tmp_path)
        caller = asyncio.create_task(svc.ensure_key())
        try:
            await asyncio.wait_for(inserted.wait(), 30)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
            if svc._audit_owners:
                await asyncio.wait(svc._audit_owners, timeout=2)
            assert not svc.unlocked and not db.in_transaction
            assert "key_loaded" not in await _names(db)
        finally:
            release.set()
