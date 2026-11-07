"""The vault service: genesis-server holds the vault's key, read from ``identity.key``.

There is no passphrase (owner ruling a4a95392): the service loads the key
(``vault_store.load_identity``) when it starts, and again whenever none is held
and a ``status`` or ``release_request`` arrives, so a ``vault migrate`` or
``vault init`` done under a running server takes effect without a restart. It
keeps the X25519 identity in memory and drops it when it no longer matches
``recipient.txt``, and at stop. The key's one user is a card release
(``payments/release.py``), and what releases a card is the owner's Telegram
tap, every payment.

Every load, refused load and drop is written to ``payment_vault_events``.
Loading fails closed: with no audit log, or if the audit write fails, the key
is not held. A refused load is recorded once per distinct reason, not on every
poll.

Protocol: one JSON line in (at most 4096 bytes), one JSON line out, then the
connection closes, over a Unix socket (``env.vault_socket_path()``, in a 0700
directory, peer uid checked with ``SO_PEERCRED``). Ops: ``status``, and the
release ops ``release_request``, ``release_status``, ``release_claim`` and
``release_cancel``. ``status`` needs only the peer's uid, so the owner's CLI
works; a release op also needs a peer that ``peer_check`` verifies as the
genesis-health MCP server. The retired ``unlock`` and ``lock`` ops are answered
with a refusal that says why. Responses never echo a request.

A client keeps its write side open until the reply arrives and sends nothing
after the request line. The service cannot tell a half-close (``shutdown``
of the write side) from a close: both read as EOF, and a ``release_claim``
whose client reads as closed is refused. Bytes sent after the request line
hide a later close from that check (``at_eof`` stays False while they sit
unread), so a claim then relies on the requester's pid check.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.util
import json
import logging
import os
import socket
import struct
import time
from datetime import UTC, datetime
from pathlib import Path

from genesis.db.crud import payment_vault_events as audit
from genesis.env import vault_socket_path
from genesis.payments import vault_migrate as vm
from genesis.payments import vault_release_store as vrs
from genesis.payments import vault_store as vs
from genesis.payments.approval import holds_long_digit_run
from genesis.payments.peer_check import PeerPolicy, PeerRefused, accept_ticks, check_release_peer
from genesis.payments.release import ReleaseManager
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

# One request line, sent by a client as soon as it connects. The bound keeps an
# idle client from holding the server open: on 3.12, Server.wait_closed() waits
# for every connected client.
_REQUEST_TIMEOUT_S = 10.0
# Handlers are short; shutdown must not stall behind one (systemd kills at 90 s,
# skipping the rest of the server's orderly shutdown).
_STOP_WAIT_S = 5.0
_MAX_REQUEST = 4096
_NO_EXTRA = "the payments extra is not installed in genesis-server"
RETIRED = vm.RETIRED  # what the retired unlock and lock ops answer


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def peer_of(writer: asyncio.StreamWriter) -> dict:
    """Who is on the other end: uid and pid from SO_PEERCRED, plus names from /proc."""
    sock = writer.get_extra_info("socket")
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", raw)
    info: dict = {"pid": pid, "uid": uid}
    with contextlib.suppress(OSError, ValueError, IndexError):
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        info["start"] = int(fields[19])  # stat field 22: start time, so a reused pid is told apart
        info["comm"] = Path(f"/proc/{pid}/comm").read_text().strip()
        info["parent"] = Path(f"/proc/{int(fields[1])}/comm").read_text().strip()
    return info


WITHHELD = "(withheld: 12 or more digits)"


def _withhold_long_runs(value, key: str | None = None):
    """``value`` with every str that holds a 12+ digit run replaced by ``WITHHELD``.

    The one guard every audit row passes, whoever wrote it: requester and /proc
    text (a host, field origins, process names, an exe path, env var names) is
    recorded as it came, so a card number in any of it must stop here. The same
    rule as the approval registry (``approval.holds_long_digit_run``), with the
    same one exemption: a ``card_ref`` value shaped exactly like a vault card ref.
    Shorter values (a CVC, an expiry) cannot be told apart from ordinary text;
    the fields that could carry one as free text are recorded only as lengths.
    """
    if isinstance(value, str):
        if key == "card_ref" and vs.CARD_REF.fullmatch(value):
            return value
        return WITHHELD if holds_long_digit_run(value) else value
    if isinstance(value, dict):
        return {k: _withhold_long_runs(v, k) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_withhold_long_runs(v) for v in value]
    return value


class VaultService:
    """The in-memory vault state and the socket that drives it.

    ``db`` must be dedicated to this service, never shared with other writers.
    Failed rollback retires and closes it, including for directly constructed services.
    """

    owned_db = None  # the connection start_vault_service opened, closed at stop

    def __init__(
        self,
        db,
        *,
        vault_path: Path | None = None,
        socket_path: Path | None = None,
        clock=None,
        approvals=None,
        release_peer=None,
    ) -> None:
        self._db = db
        self._vault = vault_path or vs.vault_dir()
        self._socket_path = socket_path or vault_socket_path()
        # CLOCK_BOOTTIME counts time the host spent suspended; the loop's
        # monotonic clock does not, so a sleep would stretch a release's deadline.
        self._clock = clock or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self._identity = None
        self._loaded_at: str | None = None
        self._load_lock = asyncio.Lock()
        self._load_refused: str | None = None  # the last refused load's reason, recorded once
        # The connection is shared by every audit write and read in this
        # service, and each write is two awaits (INSERT, COMMIT). Without this
        # lock, one write's rollback could discard another's pending INSERT, or
        # one write's COMMIT could persist a row whose own write then failed.
        self._audit_lock = asyncio.Lock()
        self._epoch = 0  # bumped when the key is dropped: a release across it is refused
        self._stopped = False
        self._server: asyncio.AbstractServer | None = None
        self._socket_ino: int | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._audit_owners: set[asyncio.Task] = set()
        self._audit_close: asyncio.Task | None = None
        # (pid, *, accept_ticks) -> Peer, raising PeerRefused. genesis-server's
        # own pid is passed explicitly: the ancestor walk looks for it to tell
        # a session it launched from a foreground one.
        self._release_peer = release_peer or functools.partial(
            check_release_peer, policy=PeerPolicy.default(), server_pid=os.getpid()
        )
        self._release = ReleaseManager(self, approvals)

    @property
    def unlocked(self) -> bool:
        return self._identity is not None

    # -- audit -------------------------------------------------------------

    async def _audit(
        self,
        event: str,
        detail: dict | None = None,
        *,
        _stopping: bool = False,
    ) -> bool:
        if self._db is None or (self._stopped and not _stopping):
            return False
        owner = tracked_task(
            self._write_audit(event, detail, _stopping),
            name="payment-vault-audit",
        )
        self._audit_owners.add(owner)
        owner.add_done_callback(self._audit_owners.discard)
        try:
            return await asyncio.shield(owner)
        except asyncio.CancelledError:
            if not owner.cancelling():
                owner.cancel()
            raise

    def _retire_audit(self) -> asyncio.Task | None:
        db, self._db = self._db, None
        if self._audit_close is None and db is not None:
            # No new statements may use this handle. Close queues behind SQL
            # already submitted; it cannot undo an already-queued COMMIT.
            self._audit_close = tracked_task(db.close(), name="payment-vault-audit-close")
            self._audit_close.add_done_callback(self._closed_audit)
        return self._audit_close

    def _closed_audit(self, task: asyncio.Task) -> None:
        self._audit_close = None

    async def _rollback_audit(self, db) -> None:
        cleanup = tracked_task(db.rollback(), name="payment-vault-audit-rollback")
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # The owner keeps the lock and the cleanup reference even
                    # when cancellation first arrives during an error rollback.
                    continue
            cleanup.result()
        except (Exception, asyncio.CancelledError) as exc:
            logger.error("payment vault audit rollback failed (%s)", type(exc).__name__)
            self._retire_audit()

    async def _write_audit(self, event: str, detail: dict | None, stopping: bool) -> bool:
        async with self._audit_lock:
            if self._db is None or (self._stopped and not stopping):
                return False
            db = self._db
            try:
                await audit.record_event(db, event, _withhold_long_runs(detail))
            except asyncio.CancelledError:
                await self._rollback_audit(db)
                raise
            except Exception as exc:  # noqa: BLE001 - the caller decides what a lost row means
                logger.error("payment vault audit write failed (%s)", type(exc).__name__)
                await self._rollback_audit(db)
                return False
        return True

    # -- state -------------------------------------------------------------

    def _recipient_matches(self, identity=None) -> bool:
        identity = identity if identity is not None else self._identity
        try:
            recipient = vs.recipient_text(self._vault)
        except vs.VaultError:
            return False
        return identity is not None and str(identity.to_public()) == recipient

    async def _drop(self, event: str, reason: str) -> None:
        """Let go of the key (a stale key, or stop): a release in flight is refused."""
        self._epoch += 1
        held = self.unlocked
        self._identity = None
        self._loaded_at = None
        self._load_refused = None
        await self._release.abort(reason)
        if held:
            await self._audit(event, {"reason": reason})

    async def ensure_key(self) -> None:
        """Hold the key if the vault has a usable one and none is held; drop a stale one.

        The load runs in a thread (it opens files and cards.db). Its audit row
        lands BEFORE the key is held, and nothing is held when it cannot be
        written. A refused load is recorded when its reason changes.
        """
        if self.unlocked and not self._recipient_matches():
            await self._drop("key_dropped", "the held key no longer matches recipient.txt")
        if self.unlocked or self._stopped or self._db is None:
            return
        async with self._load_lock:
            if self.unlocked or self._stopped:
                return
            epoch = self._epoch
            try:
                identity = await asyncio.to_thread(vrs.load_identity, self._vault)
            except vs.VaultError as exc:
                await self._load_failed(str(exc))
                return
            except ImportError:
                await self._load_failed(_NO_EXTRA)
                return
            except OSError as exc:
                await self._load_failed(f"the vault could not be read ({exc.strerror})")
                return
            if not await self._audit("key_loaded", None):
                return
            # A stop or a drop during the row, or recipient.txt replaced meanwhile:
            # not held. The row stays; the trail's next row says what happened.
            if self._stopped or epoch != self._epoch or not self._recipient_matches(identity):
                await self._audit("key_dropped", {"reason": "the vault changed during the load"})
                return
            self._identity = identity
            self._loaded_at = _now()
            self._load_refused = None

    async def _load_failed(self, reason: str) -> None:
        if reason != self._load_refused:
            self._load_refused = reason
            await self._audit("key_load_refused", {"reason": reason})

    async def status(self) -> dict:
        await self.ensure_key()
        last, audit_ok = None, self._db is not None
        if audit_ok:
            try:
                async with self._audit_lock:  # the last KEY event, not an approval row
                    audit_ok = self._db is not None
                    rows = (
                        await audit.list_recent(self._db, 1, events=audit.KEY_EVENTS)
                        if audit_ok
                        else []
                    )
                last = rows[0] if rows else None
            except Exception:  # noqa: BLE001 - reported as audit unavailable
                audit_ok = False
        try:
            initialised = vs.is_initialised(self._vault)
        except vs.VaultError:  # an unreadable vault directory: say not initialised, never crash
            initialised = False
        ready_error = None
        if initialised and not self.unlocked:
            ready_error = self._load_refused or "the key is not loaded"
        return {
            "ok": True,
            "unlocked": self.unlocked,
            "loaded_at": self._loaded_at,
            "initialised": initialised,
            "ready_error": ready_error,
            "payments_extra": importlib.util.find_spec("pyrage") is not None,
            "audit": audit_ok,
            "last_event": last,
        }

    # -- socket ------------------------------------------------------------

    async def start(self) -> None:
        vs.ensure_private_dir(self._socket_path.parent)
        # asyncio unlinks a stale socket at the path, and refuses anything else
        # there. A live competing server cannot exist: genesis-server holds its
        # ProcessLock, and the 0700 directory keeps other users out.
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._socket_path), limit=_MAX_REQUEST
        )
        try:
            os.chmod(self._socket_path, 0o600)  # the 0700 directory covers the gap since bind
            self._socket_ino = os.lstat(self._socket_path).st_ino
        except OSError:
            self._server.close()  # never leave a listener nobody tracks
            raise
        await self._audit("service_start", {"pid": os.getpid()})
        await self.ensure_key()

    async def stop(self) -> None:
        self._stopped = True
        self._epoch += 1
        had_key = self.unlocked
        self._identity = None
        self._loaded_at = None
        # Before the tasks are cancelled: the approval task is one of them.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._release.abort("the vault service stopped", stopping=True), _STOP_WAIT_S
            )
        for task in list(self._tasks):
            task.cancel()
        try:
            if self._server is not None:
                self._server.close()
                for writer in list(self._writers):
                    writer.close()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._server.wait_closed(), _STOP_WAIT_S)
            with contextlib.suppress(OSError):
                if (
                    self._socket_ino is not None
                    and os.lstat(self._socket_path).st_ino == self._socket_ino
                ):
                    os.unlink(self._socket_path)
            # The key is already dropped; a stuck write costs only its row.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._audit("service_stop", {"had_key": had_key}, _stopping=True),
                    _STOP_WAIT_S,
                )
        finally:
            for owner in self._audit_owners:
                if not owner.done() and not owner.cancelling():
                    owner.cancel()
        if self._audit_owners:
            # A timeout must not cancel rollback or give up its lock ownership.
            await asyncio.wait(self._audit_owners, timeout=_STOP_WAIT_S)

    async def _respond(self, writer: asyncio.StreamWriter, reply: dict) -> None:
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted = accept_ticks()  # first: a pid reused after connect() started later
        self._writers.add(writer)
        try:
            peer = peer_of(writer)
            if peer["uid"] != os.getuid():
                await self._audit("peer_refused", peer)
                return
            try:
                line = await asyncio.wait_for(reader.readline(), _REQUEST_TIMEOUT_S)
                request = json.loads(line)
            except (TimeoutError, ValueError, ConnectionError):  # oversize lines raise ValueError
                await self._respond(writer, {"ok": False, "error": "bad request"})
                return
            op = request.get("op") if isinstance(request, dict) else None

            def client_gone() -> bool:
                # The peer closing leaves the transport half-open, so
                # is_closing() stays False; the reader sees the EOF.
                return reader.at_eof() or writer.is_closing()

            if isinstance(op, str) and op.startswith("release_"):
                try:
                    verified = self._release_peer(peer["pid"], accept_ticks=accepted)
                except PeerRefused as exc:
                    await self._audit(
                        "release_refused",
                        {"reason": "peer not allowed", "peer": {**peer, **exc.detail}},
                    )
                    reply = {"ok": False, "error": "peer not allowed"}
                else:
                    if op == "release_request":  # a vault migrated since start is picked up
                        await self.ensure_key()
                    reply = await self._release.handle(op, request, verified, client_gone)
            elif op == "status":
                reply = await self.status()
            elif op in ("unlock", "lock"):
                reply = {"ok": False, "error": RETIRED}
            else:
                reply = {"ok": False, "error": "bad request"}
            del request, line
            await self._respond(writer, reply)
        except Exception as exc:  # noqa: BLE001 - a handler failure must not reach the server loop
            logger.error("payment vault request failed (%s)", type(exc).__name__)
        finally:
            self._writers.discard(writer)
            writer.close()


async def start_vault_service(runtime, approvals=None) -> VaultService | None:
    """Start the vault service for genesis-server; None (logged) if it cannot start.

    The audit log gets the service's OWN database connection, never the
    runtime's shared one: a failed audit commit is rolled back, and a rollback
    on the shared connection would discard other subsystems' pending writes.
    If it cannot open, the service still runs (status) and holds no key.
    ``approvals`` (a ``PaymentApprovalRegistry``) gets its audit rows written
    through the same connection; with no service it refuses every request.
    """
    from genesis.db.connection import get_db
    from genesis.env import genesis_db_path

    db = None
    try:
        db = await get_db(genesis_db_path())
    except Exception as exc:  # noqa: BLE001 - no key is then held (fail closed)
        logger.error("payment vault audit connection did not open (%s)", type(exc).__name__)
    service = VaultService(db, approvals=approvals)
    service.owned_db = db
    try:
        await service.start()
    except asyncio.CancelledError:
        service._retire_audit()
        raise
    except Exception as exc:  # noqa: BLE001 - the server runs on without a vault service
        logger.error("payment vault service did not start (%s: %s)", type(exc).__name__, exc)
        closing = service._retire_audit()
        if closing is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(closing)
        return None
    if approvals is not None:
        approvals.attach_audit(service._audit)
    return service


async def stop_vault_service(service: VaultService | None) -> None:
    if service is None:
        return
    try:
        await service.stop()
    except asyncio.CancelledError:
        service._retire_audit()
        raise
    except Exception as exc:  # noqa: BLE001 - shutdown continues regardless
        logger.error("payment vault service stop failed (%s)", type(exc).__name__)
    closing = service._retire_audit()
    if closing is not None:
        with contextlib.suppress(Exception):
            await asyncio.shield(closing)
