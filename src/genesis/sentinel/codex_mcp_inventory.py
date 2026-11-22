"""Read aggregate thread and process inventory from the same local Codex daemon."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from collections import Counter
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from re import fullmatch
from typing import TYPE_CHECKING

import aiohttp

from genesis.observability.types import Severity, Subsystem
from genesis.sentinel import codex_mcp_stop

if TYPE_CHECKING:
    from genesis.observability.events import GenesisEventBus

EVENT_TYPE = "codex.mcp_inventory"
SAMPLE_INTERVAL_S = 60
EVIDENCE_HOURS = 72
_MAX_THREADS = 256
_PAGE_SIZE = 64
_DEADLINE_S = 5
_CLOSE_GRACE_S = 0.25

@dataclass(frozen=True)
class Inventory:
    sampled_at: str
    status: str
    detail: str = ""
    daemon: tuple[str, int, int] | None = None
    version: str | None = None
    threads: frozenset[str] = frozenset()
    thread_counts: tuple[tuple[str, int], ...] = ()
    groups: frozenset[tuple[int, int]] = frozenset()
    group_counts: tuple[tuple[str, int], ...] = ()
    held_kib: int = 0
    pss_kib: int | None = None

    def summary(self) -> dict:
        """Return the aggregate evidence safe to persist, with no thread IDs or argv."""
        return {
            "sampled_at": self.sampled_at,
            "status": self.status,
            "detail": self.detail,
            "daemon": self.daemon,
            "version": self.version,
            "threads": len(self.threads) if self.status == "complete" else None,
            "thread_counts": dict(self.thread_counts) if self.status == "complete" else None,
            "groups": len(self.groups) if self.daemon is not None else None,
            "group_counts": dict(self.group_counts),
            "held_kib": self.held_kib if self.daemon is not None else None,
            "pss_kib": self.pss_kib,
        }


_latest: Inventory | None = None
_process_read: asyncio.Task | None = None


def latest() -> Inventory | None:
    return _latest


async def _read_process_data(fn, *args):
    # A deadline stops waiting for a thread, so retain its task until it is
    # done. Otherwise a stalled /proc read could accumulate once a minute.
    global _process_read  # noqa: PLW0603
    if _process_read is not None:
        if not _process_read.done():
            raise ValueError("a prior process scan is still running")
        if not _process_read.cancelled():
            _process_read.exception()
    _process_read = asyncio.create_task(asyncio.to_thread(fn, *args))
    return await asyncio.shield(_process_read)


def _control_socket() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "app-server-control" / "app-server-control.sock"


def _protocol_version(agent: object) -> str | None:
    if not isinstance(agent, str):
        return None
    parts = agent.partition("/")[2].split(maxsplit=1)
    if parts and fullmatch(r"[0-9][0-9A-Za-z.+-]{0,39}", parts[0]):
        return parts[0]
    return None


class _ThreadInventory:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.sequence = 0

    async def call(self, method: str, params: dict) -> dict:
        self.sequence += 1
        await self.ws.send_json({"id": self.sequence, "method": method, "params": params})
        while True:
            reply = await self.ws.receive_json()
            if isinstance(reply, dict) and reply.get("id") == self.sequence:
                result = reply.get("result")
                if not isinstance(result, dict):
                    raise ValueError("app-server inventory request failed")
                return result

    async def loaded(self) -> frozenset[str]:
        ids: set[str] = set()
        cursors: set[str] = set()
        cursor = None
        while True:
            page = await self.call("thread/loaded/list", {"limit": _PAGE_SIZE, "cursor": cursor})
            rows = page["data"]
            if not isinstance(rows, list) or not all(isinstance(row, str) and row for row in rows):
                raise ValueError("invalid loaded-thread page")
            if len(set(rows)) != len(rows) or ids.intersection(rows):
                raise ValueError("loaded threads changed during pagination")
            ids.update(rows)
            cursor = page.get("nextCursor")
            if len(ids) > _MAX_THREADS or len(cursors) >= _MAX_THREADS:
                raise ValueError("thread inventory exceeds diagnostic limit")
            if cursor is None:
                return frozenset(ids)
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("thread pagination did not advance")
            cursors.add(cursor)

    async def counts(self, ids: frozenset[str]) -> Counter[str]:
        counts: Counter[str] = Counter()
        for thread_id in sorted(ids):
            data = await self.call("thread/read", {"threadId": thread_id, "includeTurns": False})
            thread = data["thread"]
            if thread["id"] != thread_id:
                raise ValueError("thread metadata refers to a different thread")
            source = thread.get("source")
            if isinstance(source, dict) and "subAgent" in source:
                source = "subagent"
            if not isinstance(source, str) or source not in ("vscode", "cli", "subagent", "exec", "appServer"):
                source = "other"
            status = thread["status"]["type"]
            if status not in ("active", "idle"):
                status = "other"
            counts[f"{source}/{status}"] += 1
        return counts


def _family(group: codex_mcp_stop.Group) -> str:
    label = group.cmd.split()
    if len(label) > 1 and label[1] == "genesis_mcp_server.py":
        return "genesis"
    if label and label[0] in ("ripwire", "serena", "node", "nodejs", "codebase-memory-mcp"):
        return label[0]
    return "other"


def _proportional_memory(groups: list[codex_mcp_stop.Group]) -> int | None:
    total = 0
    try:
        for group in groups:
            for member in group.members:
                before = codex_mcp_stop.proc_io.probe_stat(member)
                if isinstance(before, str) or os.getpgid(member) != group.pgid:
                    return None
                text = (Path("/proc") / str(member) / "smaps_rollup").read_text()
                fields = next(line.split() for line in text.splitlines() if line.startswith("Pss:"))
                value = int(fields[1])
                after = codex_mcp_stop.proc_io.probe_stat(member)
                if (
                    value < 0
                    or fields[2] != "kB"
                    or isinstance(after, str)
                    or before.starttime != after.starttime
                    or os.getpgid(member) != group.pgid
                    or (member == group.pgid and after.starttime != group.starttime)
                ):
                    return None
                total += value
    except (OSError, StopIteration, ValueError, IndexError):
        return None
    return total


def _current_identity(pid: int) -> tuple[str, int, int]:
    stat = codex_mcp_stop.proc_io.probe_stat(pid)
    if isinstance(stat, str):
        raise ValueError("daemon no longer readable")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return boot, pid, stat.starttime


@asynccontextmanager
async def _inventory_socket(path: Path):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=path)) as client:
        ws = await client.ws_connect("http://localhost/", max_msg_size=1024 * 1024)
        try:
            yield ws
        finally:
            # Bound the entire handshake, even if the peer keeps sending data.
            with suppress(TimeoutError):
                await asyncio.wait_for(ws.close(), _CLOSE_GRACE_S)


async def collect() -> Inventory:
    sampled_at = datetime.now(UTC).isoformat()
    observed: dict = {}
    try:
        async with asyncio.timeout(_DEADLINE_S - _CLOSE_GRACE_S):
            scan = await _read_process_data(codex_mcp_stop.scan)
            if not scan.app_servers and not scan.uncertain:
                return Inventory(sampled_at, "absent")
            async with _inventory_socket(_control_socket()) as ws:
                peer = ws.get_extra_info("socket")
                pid, uid, _gid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                identity = await _read_process_data(_current_identity, pid)
                if uid != os.geteuid() or scan.app_server_starttimes.get(pid) != identity[2]:
                    raise ValueError("inventory socket does not belong to the scanned daemon")
                groups = [group for group in scan.groups if group.app_server_pid == pid]
                observed = {
                    "daemon": identity,
                    "groups": frozenset((group.pgid, group.starttime) for group in groups),
                    "group_counts": tuple(sorted(Counter(_family(group) for group in groups).items())),
                    "held_kib": sum(group.rss_kb for group in groups),
                }
                if scan.uncertain:
                    return Inventory(sampled_at, "partial", "process scan incomplete", **observed)
                rpc = _ThreadInventory(ws)
                initialized = await rpc.call("initialize", {
                    "clientInfo": {"name": "genesis-inventory", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                })
                observed["version"] = _protocol_version(initialized.get("userAgent"))
                await ws.send_json({"method": "initialized"})
                ids = await rpc.loaded()
                counts = await rpc.counts(ids)
                if await rpc.loaded() != ids or await _read_process_data(_current_identity, pid) != identity:
                    return Inventory(sampled_at, "partial", "inventory changed during sample", **observed)
            pss = await _read_process_data(_proportional_memory, groups)
            return Inventory(
                sampled_at, "complete", threads=ids, thread_counts=tuple(sorted(counts.items())),
                pss_kib=pss, **observed,
            )
    except (aiohttp.ClientError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return Inventory(sampled_at, "partial" if observed else "unavailable", type(exc).__name__, **observed)


async def sample(bus: GenesisEventBus) -> None:
    global _latest  # noqa: PLW0603
    if (
        os.environ.get("GENESIS_CODEX_MCP_INVENTORY_DISABLED") == "1"
        or await asyncio.to_thread(codex_mcp_stop.load_mode) == codex_mcp_stop.MODE_OFF
    ):
        _latest = None
        return
    _latest = await collect()
    if _latest.daemon is not None:
        await bus.emit(
            Subsystem.HEALTH, Severity.DEBUG, EVENT_TYPE, "Sampled Codex MCP inventory",
            **_latest.summary(),
        )


if __name__ == "__main__":
    print(json.dumps(asyncio.run(collect()).summary(), indent=2))
