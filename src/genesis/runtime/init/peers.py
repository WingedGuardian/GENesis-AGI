"""Standalone-owned peer installation; recovery precedes execution readiness."""

import asyncio
import logging
import os
import stat
from pathlib import Path
from uuid import uuid4

from genesis.env import genesis_home
from genesis.peers.recovery import PeerRecovery, recovered_binding, recovery_elapsed
from genesis.peers.registry import PeerRegistry
from genesis.peers.tasks import PeerTasks, TaskRefusal
from genesis.util.tasks import tracked_task

logger = logging.getLogger("genesis.runtime")
_POLL_INTERVAL_S = 5
# Reuse the existing direct-runner host shutdown grace, not a model deadline.
_SHUTDOWN_GRACE_S = 10


def private_directory(directory):
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError("Private peer directory required")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise ValueError("Private peer directory unavailable")
    return directory


class PeerRuntime(PeerTasks):
    def __init__(self, runtime, registry, directory):
        super().__init__(registry)
        self.runtime, self.directory = runtime, directory
        self.loop = asyncio.get_running_loop()
        self.coordinator = self.results = self.poll = self.closing = None
        self.research = None
        self.recovered = self.stopping = False
        self._stop_lock = asyncio.Lock()

    def _host_ready(self):
        return not (
            self.stopping
            or not self.recovered
            or not self.runtime.is_bootstrapped
            or self.coordinator is None
            or self.coordinator.broker._runner is None
            or self.poll is None
            or self.poll.done()
            or self.runtime._direct_session_runner._peer_cleanup_holds
        )

    async def _ready_on(self, db):
        blocked = await (
            await db.execute("SELECT 1 FROM peer_segments WHERE status='blocked' LIMIT 1")
        ).fetchone()
        settings = await (await db.execute("SELECT mode FROM peer_settings WHERE id=1")).fetchone()
        return (
            self._host_ready()
            and blocked is None
            and settings is not None
            and settings[0] != "disabled"
        )

    async def ready(self):
        if not self._host_ready():
            return False
        async with self.registry.connection() as db:
            return await self._ready_on(db)

    async def require_execution(self, db):
        # The caller holds BEGIN IMMEDIATE: mode cannot change underneath the
        # durable writer. Host pause/stopping is checked after awaited reads.
        if not await self._ready_on(db) or self.runtime.paused:
            raise TaskRefusal("not_ready", 503)

    async def execution_allowed(self):
        return await self.ready() and not self.runtime.paused

    async def admit(self, identity, message, *, work_limit_s=3600):
        if not await self.execution_allowed():
            raise TaskRefusal("not_ready", 503)
        return await self.coordinator.admit(identity, message, work_limit_s=work_limit_s)

    async def cancel(self, identity, task_id):
        return await self.coordinator.cancel(identity, task_id)

    async def health(self):
        async with self.registry.connection() as db:
            counts = await (
                await db.execute(
                    "SELECT COALESCE(SUM(slot_reserved),0),COALESCE(SUM(CASE WHEN t.state='submitted' "
                    "AND q.status='pending' THEN 1 ELSE 0 END),0) FROM peer_tasks t "
                    "JOIN direct_session_queue q ON q.id=t.queue_id"
                )
            ).fetchone()
        return dict(
            runtime_ready=self.runtime.is_bootstrapped,
            task_service_ready=await self.execution_allowed(),
            active_tasks=counts[0],
            queue_depth=counts[1],
        )

    async def _poll(self):
        while not self.stopping:
            try:
                if (await self.registry.settings())["mode"] == "disabled":
                    for binding in tuple(self.coordinator._bindings.values()):
                        self.coordinator._fence(binding)
                await self.coordinator.tick()  # Each execution chokepoint rechecks the host gate.
            except Exception:
                logger.error("Peer runtime polling failed")
                raise RuntimeError("Peer runtime polling failed") from None
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def stop(self):
        async with self._stop_lock:
            if self.stopping:
                return
            self.stopping = True
            if self.coordinator is None:
                if self.research is not None:
                    await self.research.close()
                return
            self.coordinator.quiesce()
            notifications = list(self.coordinator._notifications.values())
            if self.poll is not None:
                self.poll.cancel()
            waits = notifications + ([self.poll] if self.poll is not None else [])
            if waits:
                _, pending = await asyncio.wait(waits, timeout=_SHUTDOWN_GRACE_S)
                if pending:
                    logger.error("Peer polling or notification shutdown requires reconciliation")
            if self.closing is None:
                self.closing = tracked_task(self._close(), name="peer-runtime-close")
            _, pending = await asyncio.wait([self.closing], timeout=_SHUTDOWN_GRACE_S)
            if pending or self.runtime._direct_session_runner._peer_cleanup_holds:
                async with self.registry.connection() as db:
                    rows = await (
                        await db.execute("SELECT * FROM peer_segments WHERE status!='drained'")
                    ).fetchall()
                for row in rows:
                    binding = recovered_binding(row, self.directory / "segments")
                    elapsed, uncertain = recovery_elapsed(row)
                    await self.coordinator.state.settle(
                        binding, elapsed, clean=False, uncertain=uncertain
                    )
                logger.error("Peer scope shutdown requires reconciliation")

    async def _close(self):
        await self.coordinator.close()
        # A pending or failed scope close must not retire a client under active users.
        if self.research is not None:
            await self.research.close()


async def init(runtime):
    existing = getattr(runtime, "_peer_runtime", None)
    if existing is not None:
        if existing.loop is not asyncio.get_running_loop():
            raise RuntimeError("Peer runtime belongs to another loop")
        return
    if runtime._db is None:
        return
    rows = await (await runtime._db.execute("PRAGMA database_list")).fetchall()
    path = next((row[2] for row in rows if row[1] == "main" and row[2]), None)
    if path is None:
        raise RuntimeError("Peer runtime requires persistent private storage")
    registry = PeerRegistry(path)
    directory = private_directory(genesis_home() / "peers")
    private_directory(directory / "segments")
    service = PeerRuntime(runtime, registry, directory)
    runtime._peer_runtime = service  # Own partial setup, including degraded bootstrap.
    service.recovered = await PeerRecovery(registry, directory / "segments").run()
    if (
        not service.recovered
        or not runtime.is_bootstrapped
        or runtime._direct_session_runner is None
        or runtime._session_manager is None
        or runtime._approval_manager is None
        or runtime._autonomous_cli_approval_gate is None
        or (await registry.settings())["mode"] == "disabled"
    ):
        return
    from genesis.peers.artifacts import PeerArtifacts
    from genesis.peers.coordinator import PeerCoordinator
    from genesis.peers.runner import _ceiling

    await _ceiling(runtime)
    service.results = PeerArtifacts(registry, directory / "segments")
    coordinator = PeerCoordinator(
        registry,
        runtime._direct_session_runner,
        runtime._approval_manager,
        runtime._autonomous_cli_approval_gate,
        directory,
        service.results.publish,
        execution_allowed=service.execution_allowed,
        execution_gate=service.require_execution,
    )
    service.coordinator = coordinator
    try:
        from genesis.peers.research import PeerResearch

        service.research = PeerResearch(coordinator.broker)
        service.results.research = service.research
        broker_root = private_directory(directory / "broker")
        await coordinator.start(broker_directory=broker_root / uuid4().hex)
        runtime._peer_session_lifecycle = coordinator
        service.poll = tracked_task(service._poll(), name="peer-runtime-poll")
    except BaseException:
        await service.stop()
        runtime._peer_session_lifecycle = None
        raise
