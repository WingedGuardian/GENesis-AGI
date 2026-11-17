"""Local owner control for one persistent agent; no network/model initiators.

The trust boundary is the OS user, not speaker verification: processes running
as that user already have shell access. Intended consumer: live_agent_attach.py.
This interface must not be exposed to voice models or remote callers.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import socket
import stat
import struct
from dataclasses import asdict
from pathlib import Path
from typing import Any

from genesis.live_agent.agent import LiveAgent
from genesis.live_agent.wire import read_line
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)
# At the adapter's 0.2s flush cadence this retains about 25s of progress for
# a slow reader. It is a delivery backlog, not history: disconnect on overflow.
_BACKLOG = 128


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    peer = writer.get_extra_info("socket")
    return struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode() + b"\n"


class _Controller:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_BACKLOG)

    def offer(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("live-agent controller too slow; disconnecting without replay")
            self.abort()

    def abort(self) -> None:
        self.writer.transport.abort()

    async def send(self) -> None:
        try:
            while True:
                self.writer.write(encode(await self.queue.get()))
                await self.writer.drain()
        finally:
            self.abort()


class LocalHost:
    def __init__(self, agent: LiveAgent, socket_path: Path):
        self.agent = agent
        self.socket_path = socket_path
        self.ready = asyncio.Event()
        self._controller: _Controller | None = None
        self._handlers: set[asyncio.Task] = set()
        self._inode: tuple[int, int] | None = None
        self._closing = False

    @property
    def controller_connected(self) -> bool:
        return self._controller is not None

    def _prepare_directory(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("control directory must be owned by this user with mode 0700")

    def _remove_stale_socket(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise FileExistsError("control path exists and is not an owned socket")
        # Endpoint lock is held across this removal, bind and final cleanup.
        # A live socket not owned by this host must never be displaced.
        with socket.socket(socket.AF_UNIX) as probe:
            probe.setblocking(False)
            try:
                probe.connect(str(self.socket_path))
            except ConnectionRefusedError:
                self.socket_path.unlink()
                return
        raise FileExistsError("control socket is already listening")

    def _unlink_owned_socket(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if self._inode == (info.st_dev, info.st_ino) and stat.S_ISSOCK(info.st_mode):
            self.socket_path.unlink()

    async def run(self, stop: asyncio.Event) -> None:
        self._prepare_directory()
        # Never unlink flock files: a successor must lock the same inode.
        with self.socket_path.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("control socket is already owned") from None
            self._remove_stale_socket()
            await self._run_locked(stop)

    async def _run_locked(self, stop: asyncio.Event) -> None:
        server = None
        tasks = []
        sock = socket.socket(socket.AF_UNIX)
        try:
            await self.agent.start()
            # Bind ourselves: asyncio's path= convenience may remove a socket.
            sock.bind(str(self.socket_path))
            info = self.socket_path.lstat()
            self._inode = (info.st_dev, info.st_ino)
            self.socket_path.chmod(0o600)
            sock.setblocking(False)
            server = await asyncio.start_unix_server(self._accept, sock=sock)
            tasks = [
                tracked_task(self._events(), name="live_agent_host.events", logger=logger),
                tracked_task(stop.wait(), name="live_agent_host.stop", logger=logger),
            ]
            self.ready.set()
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[0].done():
                tasks[0].result()
                logger.warning("live-agent stream disconnected; closing host")
        finally:
            self._closing = True
            if server:
                server.close()
            sock.close()
            for task in [*tasks, *self._handlers]:
                task.cancel()
            await asyncio.gather(*tasks, *self._handlers, return_exceptions=True)
            if server:
                # Python 3.12 waits for clients too; abort them BEFORE waiting.
                await server.wait_closed()
            try:
                await self.agent.close()
            finally:
                self._unlink_owned_socket()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.transport.abort()
            return
        task = tracked_task(self._serve(reader, writer), name="live_agent_host.controller", logger=logger)
        self._handlers.add(task)

        def finished(done):
            # A task cancelled before first execution never enters _serve's
            # finally. The accept callback owns the transport until then too.
            writer.transport.abort()
            self._handlers.discard(done)

        task.add_done_callback(finished)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        controller = None
        sender = None
        try:
            if _peer_uid(writer) != os.getuid():
                return
            if self._controller is not None:
                writer.write(encode({"type": "response", "ok": False, "error": "another controller is attached"}))
                await writer.drain()
                return
            controller = self._controller = _Controller(writer)
            controller.offer({"type": "snapshot", "history": "not replayed", "status": self.agent.status()})
            sender = tracked_task(controller.send(), name="live_agent_host.send", logger=logger)
            while line := await read_line(reader):
                try:
                    message = json.loads(line)
                    response = await self._command(message)
                except (ValueError, TypeError, PermissionError, RuntimeError) as exc:
                    response = {"type": "response", "id": None, "ok": False, "error": str(exc)}
                controller.offer(response)
        except (ConnectionError, OSError, ValueError):
            logger.info("local controller disconnected", exc_info=True)
        finally:
            if controller is self._controller:
                self._controller = None
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            writer.transport.abort()

    async def _command(self, message: Any) -> dict:
        if not isinstance(message, dict) or not isinstance(message.get("id"), str) or not message["id"]:
            raise ValueError("command requires a nonempty string id")
        response = {"type": "response", "id": message["id"], "ok": False}
        try:
            if message.get("initiator", "owner_cli") != "owner_cli":
                raise PermissionError("only local owner_cli controls are accepted")
            response["result"] = await self._dispatch(message)
            response["ok"] = True
        except Exception as exc:
            # Keep a bad command local to this request; never report success.
            response["error"] = f"{type(exc).__name__}: {exc}"
        return response

    async def _dispatch(self, message: dict) -> Any:
        op = message.get("op")
        if op == "status":
            return self.agent.status()
        if op == "interrupt":
            await self.agent.interrupt()
            return None
        if op in ("turn", "steer"):
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("turn/steer requires nonempty text")
            return asdict(await self.agent.turn(text, cancel_pending=op == "steer"))
        if op == "answer":
            ask = message.get("approval_id")
            allow = message.get("allow")
            if not isinstance(ask, str) or not ask or type(allow) is not bool:
                raise ValueError("answer requires approval_id and a boolean allow")
            if not self.agent.answer(ask, allow):
                raise ValueError("approval is no longer pending")
            return None
        raise ValueError("unknown control operation")

    async def _events(self) -> None:
        # Always drain, including while the owner is detached. No event journal.
        async for event in self.agent.events():
            if self._controller:
                self._controller.offer({"type": "event", **asdict(event)})
            if event.kind == "disconnected":
                return
