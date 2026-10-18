"""The vault service: genesis-server holds the vault's key between unlock and lock.

``python -m genesis vault unlock`` sends the passphrase over a Unix socket
(``env.vault_socket_path()``, in a 0700 directory, peer uid checked with
``SO_PEERCRED``). The service unwraps ``identity.age`` and keeps the X25519
identity in memory. It relocks on ``vault lock``, after ``IDLE_RELOCK_S``
idle, when the held key no longer matches ``recipient.txt``, and on every
restart (the key is never written anywhere). Nothing decrypts a card yet; the
first user of the key is the card-fill flow, which approval gates.

The unwrap (age scrypt, about 1-2 s of CPU) runs in a short-lived subprocess
that imports only pyrage, so the event loop never waits on it. MEASURED on
pyrage 1.4.0 in this venv: ``passphrase.decrypt`` in a thread stalled the loop
for its whole run in 6 of 6 tries (1.2-2.0 s), and a subprocess stalled it for
1 ms. One reviewer's run saw no stall from the thread; a subprocess makes the
answer independent of how a pyrage build handles the GIL. The passphrase goes to
it on stdin, never argv.

Every unlock, lock, refusal and relock is written to ``payment_vault_events``
with the requesting process's pid, start time and names. Unlock fails closed:
with no audit log, or if the audit write fails, the key is not held. Lock
always drops the key, whatever happens to its audit row.

Protocol: one JSON line in (at most 4096 bytes), one JSON line out, then the
connection closes. Ops: ``status``, ``unlock`` (``passphrase``), ``lock``.
Responses never echo a request.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import socket
import struct
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from genesis.db.crud import payment_vault_events as audit
from genesis.env import vault_socket_path
from genesis.payments import vault_store as vs
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

IDLE_RELOCK_S = 30 * 60
# How often an unlocked service compares the suspend-aware relock deadline:
# after a resume the key is dropped within this much loop time.
_IDLE_CHECK_S = 60.0
# One request line, sent by the CLI as soon as it connects (it reads the
# passphrase first). The bound keeps an idle client from holding the server
# open: on 3.12, Server.wait_closed() waits for every connected client.
_REQUEST_TIMEOUT_S = 10.0
# scrypt takes about 2-5 s here; a child still running at 60 s is hung, and
# the unlock lock it holds would block every later unlock.
_UNWRAP_TIMEOUT_S = 60.0
# Handlers are short; shutdown must not stall behind one (systemd kills at 90 s,
# skipping the rest of the server's orderly shutdown).
_STOP_WAIT_S = 5.0
_MAX_REQUEST = 4096

# Run as `python -I -c`: isolated from PYTHONPATH and the user site, importing
# nothing but pyrage. Exit 10 = decryption failed (a wrong passphrase, or a
# damaged file body: age cannot tell the two apart), 3 = pyrage missing, 4 = the
# file could not be read, is a symlink or not a regular file, is larger than a
# sealed key can be, or is not an age file (anything else that went wrong).
# Not 1: an interpreter that dies before running this exits 1, which must not
# read as a wrong passphrase. The read is capped like
# the store's: a sealed X25519 key is well under 4096 bytes.
_UNWRAP = r"""
import os
import stat
import sys
try:
    from pyrage import DecryptError, passphrase
except ImportError:
    sys.exit(3)
try:
    with os.fdopen(os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            sys.exit(4)
        sealed = f.read(4097)
    if len(sealed) > 4096 or not sealed.startswith(b"age-encryption.org/v1"):
        sys.exit(4)
    key = passphrase.decrypt(sealed, sys.stdin.buffer.read().decode("utf-8"))
except DecryptError:
    sys.exit(10)
except BaseException:  # also a pyo3 panic, which is not an Exception; exit 10 stays DecryptError's
    sys.exit(4)
try:
    sys.stdout.write(key.decode())
except BaseException:
    sys.exit(4)
"""
_UNWRAP_REFUSALS = {
    10: "wrong passphrase (or a damaged identity.age)",
    3: "the payments extra is not installed in genesis-server",
    4: "identity.age could not be read",
    124: "the unwrap timed out",
    127: "the unwrap helper could not start",
}


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
        idle_relock_s: float = IDLE_RELOCK_S,
        idle_check_s: float = _IDLE_CHECK_S,
        clock=None,
        python: str = sys.executable,
    ) -> None:
        self._db = db
        self._vault = vault_path or vs.vault_dir()
        self._socket_path = socket_path or vault_socket_path()
        self._idle_relock_s = idle_relock_s
        self._idle_check_s = idle_check_s
        # CLOCK_BOOTTIME counts time the host spent suspended; the loop's
        # monotonic clock does not, so a sleep would stretch the unlock window.
        self._clock = clock or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self._python = python
        self._identity = None
        self._unlocked_at: str | None = None
        self._relock_handle: asyncio.TimerHandle | None = None
        self._relock_at: float | None = None  # on self._clock
        self._unlock_lock = asyncio.Lock()
        # The connection is shared by every audit write and read in this
        # service, and each write is two awaits (INSERT, COMMIT). Without this
        # lock, one write's rollback could discard another's pending INSERT, or
        # one write's COMMIT could persist a row whose own write then failed.
        self._audit_lock = asyncio.Lock()
        self._epoch = 0  # bumped by every lock: an unlock in flight across one is discarded
        self._stopped = False
        self._server: asyncio.AbstractServer | None = None
        self._socket_ino: int | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._audit_owners: set[asyncio.Task] = set()
        self._audit_close: asyncio.Task | None = None

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
        _correction: bool = False,
    ) -> bool:
        if self._db is None or (self._stopped and not _stopping):
            return False
        owner = tracked_task(
            self._write_audit(event, detail, _stopping, _correction),
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

    async def _write_audit(
        self,
        event: str,
        detail: dict | None,
        stopping: bool,
        _correction: bool = False,
    ) -> bool:
        async with self._audit_lock:
            if self._db is None or (self._stopped and not stopping):
                return False
            if _correction and self.unlocked:
                return False
            db = self._db
            try:
                await audit.record_event(db, event, detail)
            except asyncio.CancelledError:
                await self._rollback_audit(db)
                raise
            except Exception as exc:  # noqa: BLE001 - the caller decides what a lost row means
                logger.error("payment vault audit write failed (%s)", type(exc).__name__)
                await self._rollback_audit(db)
                return False
        return True

    async def _correct_trail(self) -> None:
        """If the trail ends on "unlock" while no key is held, record that it is locked.

        A row can be lost when the database fails: the correction after an
        aborted unlock, or a lock's own row (a lock always drops the key). The
        truth is derived from the trail itself, so nothing has to remember it.
        Callers make sure no unlock is between its row and holding the key.
        """
        if self._db is None or self.unlocked:
            return
        try:
            async with self._audit_lock:
                if self._db is None:
                    return
                rows = await audit.list_recent(self._db, 1)
        except Exception:  # noqa: BLE001 - status reports the audit log as unavailable
            return
        if not self.unlocked and rows and rows[0]["event"] == "unlock":
            await self._audit(
                "lock",
                {"correction": "no key is held; an earlier row was lost"},
                _correction=True,
            )

    # -- state -------------------------------------------------------------

    def _recipient_matches(self, identity=None) -> bool:
        identity = identity if identity is not None else self._identity
        try:
            recipient = vs.recipient_text(self._vault)
        except vs.VaultError:
            return False
        return identity is not None and str(identity.to_public()) == recipient

    def _superseded(self, epoch: int, client_gone) -> str | None:
        """Why an unlock in flight must not take effect, or None."""
        if self._stopped:
            return "the vault service stopped during the unlock"
        if epoch != self._epoch:
            return "the vault was locked during the unlock"
        if client_gone():
            return "the client left"
        return None

    def _drop(self) -> None:
        self._identity = None
        self._unlocked_at = None
        self._relock_at = None
        if self._relock_handle is not None:
            self._relock_handle.cancel()
            self._relock_handle = None

    def _arm_relock(self, deadline: float) -> None:
        self._relock_at = deadline
        self._schedule_idle_check()

    def _schedule_idle_check(self) -> None:
        """Wake at most every ``idle_check_s`` (loop time) to compare the suspend-aware deadline."""
        if self._relock_handle is not None:
            self._relock_handle.cancel()
        remaining = max(0.0, self._relock_at - self._clock())
        self._relock_handle = asyncio.get_running_loop().call_later(
            min(remaining, self._idle_check_s), self._idle_check
        )

    def _idle_check(self) -> None:
        self._relock_handle = None
        if self._relock_at is None:
            return
        if self._clock() < self._relock_at:
            self._schedule_idle_check()
            return
        task = tracked_task(self.lock(None, event="idle_relock"), name="payment-vault-idle-relock")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _unwrap(self, passphrase: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._python,
                "-I",
                "-c",
                _UNWRAP,
                str(self._vault / vs.IDENTITY),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={},  # the child needs nothing from the server's environment (API keys live there)
            )
        except OSError:  # no memory or processes, or the interpreter moved after an upgrade
            return 127, ""
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(passphrase.encode()), _UNWRAP_TIMEOUT_S
            )
        except TimeoutError:
            return 124, ""
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return proc.returncode, out.decode() if proc.returncode == 0 else ""

    async def status(self) -> dict:
        if self.unlocked and not self._recipient_matches():
            await self.lock(None, event="stale_identity_relock")
        if not self._unlock_lock.locked():  # never mid-unlock, between its row and the key
            await self._correct_trail()
        last, audit_ok = None, self._db is not None
        if audit_ok:
            try:
                async with self._audit_lock:
                    audit_ok = self._db is not None
                    rows = await audit.list_recent(self._db, 1) if audit_ok else []
                last = rows[0] if rows else None
            except Exception:  # noqa: BLE001 - reported as audit unavailable
                audit_ok = False
        try:
            initialised = vs.is_initialised(self._vault)
        except vs.VaultError:  # an unreadable vault directory: say not initialised, never crash
            initialised = False
        # The whole of check_ready, so `vault unlock` refuses a vault the
        # service would refuse before it reads the passphrase.
        ready_error = None
        if initialised and not self.unlocked:
            try:
                await asyncio.to_thread(vs.check_ready, self._vault)
            except vs.VaultError as exc:
                ready_error = str(exc)
            except ImportError:
                ready_error = _UNWRAP_REFUSALS[3]
            except OSError as exc:
                ready_error = f"the vault could not be checked ({exc.strerror})"
        relock_in = None
        if self._relock_at is not None:
            relock_in = max(0, round(self._relock_at - self._clock()))
        return {
            "ok": True,
            "unlocked": self.unlocked,
            "unlocked_at": self._unlocked_at,
            "relock_in_s": relock_in,
            "initialised": initialised,
            "ready_error": ready_error,
            "payments_extra": importlib.util.find_spec("pyrage") is not None,
            "audit": audit_ok,
            "last_event": last,
        }

    async def _refuse(self, reason: str, peer: dict | None) -> dict:
        await self._audit("unlock_refused", {"reason": reason, "peer": peer})
        return {"ok": False, "error": reason}

    async def unlock(self, passphrase: str, peer: dict | None, client_gone=lambda: False) -> dict:
        # Taken on arrival, not after waiting for the unlock lock: a lock that
        # lands while this request waits behind another unlock still wins.
        epoch = self._epoch
        async with self._unlock_lock:
            async with self._audit_lock:
                if self._db is None:
                    return {
                        "ok": False,
                        "error": "the audit log is unavailable; the vault stays locked",
                    }
            if self._stopped:
                return {"ok": False, "error": "the vault service is stopping"}
            if self.unlocked:  # checked first: a held key is never answered with a refusal
                return {"ok": True, "state": "already unlocked"}
            await self._correct_trail()
            try:
                # Off the loop: it stats and opens files and cards.db.
                await asyncio.to_thread(vs.check_ready, self._vault)
            except vs.VaultError as exc:
                return await self._refuse(str(exc), peer)
            except ImportError:  # check_ready parses recipient.txt with pyrage
                return await self._refuse(_UNWRAP_REFUSALS[3], peer)
            except OSError as exc:  # never an unaudited, unanswered unlock
                return await self._refuse(f"the vault could not be checked ({exc.strerror})", peer)
            why = self._superseded(epoch, client_gone)
            if why:
                return await self._refuse(why, peer)
            code, key = await self._unwrap(passphrase)
            if code != 0:
                return await self._refuse(
                    _UNWRAP_REFUSALS.get(code, "the key could not be unwrapped"), peer
                )
            why = self._superseded(epoch, client_gone)
            if why:
                return await self._refuse(why, peer)
            from pyrage import IdentityError, x25519

            try:
                identity = x25519.Identity.from_str(key)
            except (IdentityError, ValueError):
                return await self._refuse("identity.age does not hold an age X25519 key", peer)
            finally:
                del key
            if not self._recipient_matches(identity):
                return await self._refuse("identity.age does not match recipient.txt", peer)
            # The audit row lands BEFORE the key is held. A lock, stop,
            # disconnect or key-file change during that write is checked again
            # after it, and a second row records the abort so the trail ends in
            # the true state.
            if not await self._audit("unlock", {"peer": peer}):
                return {"ok": False, "error": "the audit write failed; the vault stays locked"}
            why = self._superseded(epoch, client_gone)
            if why is None and not self._recipient_matches(identity):
                why = "recipient.txt changed during the unlock"
            if why:
                # The "unlock" row is committed. If this refusal row is lost
                # too, the next status or unlock records the lock
                # (_correct_trail), so the trail does not end on "unlock".
                return await self._refuse(why, peer)
            deadline = self._clock() + self._idle_relock_s  # before the key is held
            self._identity = identity
            self._unlocked_at = _now()
            self._arm_relock(deadline)
            return {"ok": True, "state": "unlocked", "relock_in_s": round(self._idle_relock_s)}

    async def lock(self, peer: dict | None, *, event: str = "lock") -> dict:
        self._epoch += 1
        was_unlocked = self.unlocked
        self._drop()
        if not was_unlocked:
            if peer:  # every lock request is on the trail, even one with nothing to drop
                await self._audit(event, {"peer": peer, "state": "already locked"})
            return {"ok": True, "state": "already locked"}
        await self._audit(event, {"peer": peer} if peer else None)
        return {"ok": True, "state": "locked"}

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

    async def stop(self) -> None:
        self._stopped = True
        self._epoch += 1
        was_unlocked = self.unlocked
        self._drop()
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
                    self._audit("service_stop", {"was_unlocked": was_unlocked}, _stopping=True),
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
            if op == "status":
                reply = await self.status()
            elif op == "lock":
                reply = await self.lock(peer)
            elif (
                op == "unlock"
                and isinstance(request.get("passphrase"), str)
                and request["passphrase"]
            ):
                reply = await self.unlock(
                    request["passphrase"],
                    peer,
                    # The peer closing leaves the transport half-open, so
                    # is_closing() stays False; the reader sees the EOF.
                    client_gone=lambda: reader.at_eof() or writer.is_closing(),
                )
            else:
                reply = {"ok": False, "error": "bad request"}
            del request, line
            await self._respond(writer, reply)
        except Exception as exc:  # noqa: BLE001 - a handler failure must not reach the server loop
            logger.error("payment vault request failed (%s)", type(exc).__name__)
        finally:
            self._writers.discard(writer)
            writer.close()


async def start_vault_service(runtime) -> VaultService | None:
    """Start the vault service for genesis-server; None (logged) if it cannot start.

    The audit log gets the service's OWN database connection, never the
    runtime's shared one: a failed audit commit is rolled back, and a rollback
    on the shared connection would discard other subsystems' pending writes.
    If it cannot open, the service still runs (status, lock) and refuses unlock.
    """
    from genesis.db.connection import get_db
    from genesis.env import genesis_db_path

    db = None
    try:
        db = await get_db(genesis_db_path())
    except Exception as exc:  # noqa: BLE001 - unlock then refuses (fail closed)
        logger.error("payment vault audit connection did not open (%s)", type(exc).__name__)
    service = VaultService(db)
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
