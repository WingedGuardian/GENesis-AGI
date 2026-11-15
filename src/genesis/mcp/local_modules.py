"""Opt-in ownership of one local frontend's external-module resources."""

import asyncio
import math
import sys
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import anyio

from genesis.util.proc_kill import LaunchRecord, confirm_process_exit, spawn_owned
from genesis.util.tasks import tracked_task


class ModuleOwnershipError(RuntimeError):
    """Admission or resource settlement could not be confirmed."""


class ModuleCleanupUnconfirmed(ModuleOwnershipError):
    """Actual owned cleanup is uncertain and takes precedence over cancellation."""


class ModuleOperationStopped(ModuleOwnershipError):
    """The owner stopped an admitted operation during frontend drainage."""


@dataclass
class _Unit:
    finished: asyncio.Future
    cancel_scope: anyio.CancelScope
    uncertain: bool = False


@dataclass(eq=False)
class _Resource:
    unit: object
    persistent: bool
    record: LaunchRecord | None = None
    launch: asyncio.Task | None = None
    client: object = None
    cleanup: asyncio.Task | None = None
    observer: asyncio.Task | None = None
    deadline: float | None = None
    uncertain: bool = False
    users: set = field(default_factory=set)
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        self.idle.set()


def _task(coro):
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
    return task


class ModuleResourceOwner:
    """Retain unknown work; a reporting deadline is never proof of closure.

    The 30s post-cancel policy is owner-approved. It does not time normal work
    or promise hard shutdown/crash recovery. No JSON-RPC identifiers live here.
    """

    def __init__(self, *, grace_s, cleanup_timeout_s=30.0):
        for value in (grace_s, cleanup_timeout_s):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('module cleanup durations must be finite and nonnegative')
        self.grace_s, self.cleanup_timeout_s = grace_s, cleanup_timeout_s
        self.adapters = None
        self._units, self._resources = {}, set()
        self._unit_deadlines = {}
        self._current = ContextVar('module_resource_unit', default=None)
        self._closing = self._canceling = self.closed = False
        self._drain_task = self._deadline = self._grace_deadline = None
        self._settlement_task = None
        self._observers = set()

    def _admit(self):
        if self._closing:
            raise ModuleOwnershipError('Module owner is closing or closed')
        self._check_quarantine()

    def _check_quarantine(self):
        if any(r.uncertain for r in self._resources) or any(u.uncertain for u in self._units.values()):
            raise ModuleCleanupUnconfirmed('Module cleanup is unconfirmed')

    def require_unit(self):
        task = asyncio.current_task()
        if self._canceling or self._current.get() is not task or task not in self._units:
            raise ModuleOwnershipError('Module acquisition requires an active resource unit')
        self._check_quarantine()
        return task

    @contextmanager
    def use(self, client):
        task = self.require_unit()
        resource = next((r for r in self._resources if client is not None and r.client is client), None)
        if resource is None:
            raise ModuleOwnershipError('Client is not owned by this module generation')
        if resource.cleanup is not None:
            raise ModuleOwnershipError('Module client is retiring')
        nested = task in resource.users
        resource.users.add(task)
        resource.idle.clear()
        try:
            yield
        finally:
            if not nested:
                resource.users.remove(task)
            if not resource.users:
                resource.idle.set()

    @asynccontextmanager
    async def unit(self):
        task = asyncio.current_task()
        if self._current.get() is task and task in self._units:
            yield
            return
        self._admit()
        scope = anyio.CancelScope()
        finished = asyncio.get_running_loop().create_future()
        self._units[task] = _Unit(finished, scope)
        token = self._current.set(task)
        try:
            with scope:
                yield
            if scope.cancel_called:
                raise ModuleOperationStopped('Module operation stopped during frontend drainage')
        finally:
            try:
                await self._retire_all([r for r in self._resources if r.unit is task and not r.persistent], final=True)
            finally:
                self._current.reset(token)
                self._units.pop(task)
                self._unit_deadlines.pop(task, None)
                finished.set_result(None)

    async def run(self, operation):
        """Run module readiness/IPC in its own unit, retaining caller cancellation.

        This boundary is for the closed module callback, not arbitrary tools
        borrowing SDK request-scoped dependencies. Copied context is not unit
        membership; only nesting in the admitted task reuses its unit.
        """
        current = asyncio.current_task()
        if self._current.get() is current and current in self._units:
            return await operation()

        async def invoke():
            try:
                async with self.unit():
                    return await operation()
            except asyncio.CancelledError:
                raise ModuleOperationStopped('Module operation stopped during frontend drainage') from None

        child = _task(invoke())
        try:
            return await asyncio.shield(child)
        except asyncio.CancelledError:
            # Publish the SAME deadline to child finalization before cancelling.
            # A cumulative cancellation count cannot identify caller provenance.
            deadline = self._budget()
            deadline = min(deadline, self._unit_deadlines.get(child, deadline),
                           *(r.deadline for r in self._resources
                             if r.unit is child and r.deadline is not None))
            unit = self._units.get(child)
            if unit is not None:
                self._unit_deadlines[child] = deadline
            child.cancel()
            await self._wait({child}, deadline)
            if not child.done():
                if unit is not None:
                    unit.uncertain = True
                for resource in self._resources:
                    if resource.unit is child or child in resource.users:
                        resource.uncertain = True
                raise ModuleCleanupUnconfirmed('Module operation cleanup is unconfirmed') from None
            if not child.cancelled():
                error = child.exception()
                if isinstance(error, ModuleCleanupUnconfirmed):
                    raise error from None
            raise

    def _budget(self, *, final=False):
        deadline = asyncio.get_running_loop().time() + self.cleanup_timeout_s
        task = asyncio.current_task()
        if task in self._units:
            # A handled request still contributes to Task.cancelling(). Only
            # actual cancellation cleanup/finalization starts this allowance.
            if final or isinstance(sys.exception(), asyncio.CancelledError):
                self._unit_deadlines.setdefault(task, deadline)
            deadline = self._unit_deadlines.get(task, deadline)
        return min(deadline, self._deadline) if self._deadline is not None else deadline

    async def spawn(self, *args, persistent=False, **kwargs):
        resource = _Resource(self.require_unit(), persistent, record=LaunchRecord())
        self._resources.add(resource)
        resource.launch = _task(spawn_owned(*args, start_new_session=True,
                                           launch_record=resource.record, **kwargs))
        try:
            process = await asyncio.shield(resource.launch)
            if persistent:
                resource.observer = tracked_task(self._watch_exit(resource), name='module-process-exit')
                self._observers.add(resource.observer)
                resource.observer.add_done_callback(self._observers.discard)
            return process
        except BaseException:
            await self._retire_all([resource])
            raise

    async def _watch_exit(self, resource):
        try:
            # A pending Process.wait can remain blocked by inherited/paused
            # pipes after leader exit. Public returncode is a trigger, not proof.
            while resource in self._resources and resource.record.process.returncode is None:
                await asyncio.sleep(.1)
            if resource in self._resources:
                await self._retire_all([resource])
        except BaseException:
            if resource in self._resources:
                resource.uncertain = True
            raise

    def retain_client(self, client):
        resource = _Resource(self.require_unit(), True, client=client)
        self._resources.add(resource)

    async def retire(self, process):
        if process is not None:
            await self._retire_all([r for r in self._resources if
                (r.record is not None and r.record.process is process) or r.client is process])

    async def _wait(self, tasks, deadline):
        if not tasks:
            return False
        interrupted = False
        with anyio.CancelScope(shield=True):
            waiter = _task(asyncio.wait(tasks, timeout=max(0, deadline - asyncio.get_running_loop().time())))
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    interrupted = True
            waiter.result()
        return interrupted

    def _interrupt_launch(self, resource):
        if resource.record is not None and resource.record.task is not None and not resource.record.task.done():
            resource.record.task.cancel()
        if resource.launch is not None and not resource.launch.done():
            resource.launch.cancel()

    async def _retire(self, resource, deadline):
        if resource.client is not None:
            # HTTPX sets is_closed BEFORE awaited transport closure. A failed
            # aclose must never be certified by retrying that closed-flag no-op.
            await resource.idle.wait()
            await resource.client.aclose()
        else:
            launch, record = resource.launch, resource.record
            if not launch.done():
                launch.cancel()
                await asyncio.wait({launch}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
            if not launch.done():
                self._interrupt_launch(resource)
                raise ModuleCleanupUnconfirmed('Module creation is unconfirmed')
            if not launch.cancelled():
                launch.exception()
            if not record._claimed and launch.cancelled():
                record.confirmed = True  # managed coroutine never entered
            if not record.confirmed:
                if record.process is None:
                    raise ModuleCleanupUnconfirmed('Module creation is unconfirmed')
                await confirm_process_exit(record.process,
                    confirm_timeout_s=max(0, deadline - asyncio.get_running_loop().time()),
                    terminate=asyncio.get_running_loop().time() < deadline)
                record.confirmed = True
        self._resources.discard(resource)
        if resource.observer is not None and not resource.observer.done():
            resource.observer.cancel()

    async def _retire_all(self, resources, *, deadline=None, final=False):
        deadline = self._budget(final=final) if deadline is None else deadline
        for resource in resources:
            if resource not in self._resources:
                continue
            resource.deadline = deadline if resource.deadline is None else min(resource.deadline, deadline)
            previous = resource.cleanup
            if previous is None or (previous.done() and not previous.cancelled() and previous.exception() is not None
                                    and resource.record is not None and resource.record.process is not None):
                resource.cleanup = _task(self._retire(resource, resource.deadline))
        interrupted = await self._wait({r.cleanup for r in resources if r.cleanup is not None}, deadline)
        task = asyncio.current_task()
        if interrupted and task in self._units:
            # Cancellation may arrive after ordinary retirement chose its
            # deadline. Finalization must retain that allowance, not renew it.
            self._unit_deadlines[task] = min(deadline, self._unit_deadlines.get(task, deadline))
        unknown = [r for r in resources if r in self._resources]
        for resource in unknown:
            resource.uncertain = True
            self._interrupt_launch(resource)
        if unknown:
            raise ModuleCleanupUnconfirmed('Module cleanup is unconfirmed')
        if interrupted:
            raise asyncio.CancelledError

    def begin_draining(self):
        if asyncio.current_task() in self._units:
            raise ModuleOwnershipError('Leave the resource unit before closing')
        self._closing = True
        if self._grace_deadline is None:
            self._grace_deadline = asyncio.get_running_loop().time() + self.grace_s
        if self._drain_task is None:
            self._drain_task = _task(self._drain())

    async def _drain(self):
        await self._wait({unit.finished for unit in self._units.values()}, self._grace_deadline)
        self._canceling = True
        if self._deadline is None:
            self._deadline = asyncio.get_running_loop().time() + self.cleanup_timeout_s
        for unit in tuple(self._units.values()):
            unit.cancel_scope.cancel()
        # Start independent retirement immediately. Client tasks wait for
        # their actual borrowers, not every unrelated unit in the frontend.
        if self._settlement_task is None or self._settlement_task.done():
            self._settlement_task = _task(self._retire_all(list(self._resources), deadline=self._deadline))
        await self._wait({unit.finished for unit in self._units.values()} | {self._settlement_task}, self._deadline)
        if self._units or not self._settlement_task.done():
            for resource in self._resources:
                resource.uncertain = True
                self._interrupt_launch(resource)
            raise ModuleCleanupUnconfirmed('Module units have unconfirmed cleanup')
        self._settlement_task.result()
        await self._wait(set(self._observers), self._deadline)
        if any(not task.done() for task in self._observers):
            raise ModuleCleanupUnconfirmed('Module observers have unconfirmed cleanup')
        self.closed = True

    async def close(self):
        self.begin_draining()
        if self._drain_task.done() and not self._drain_task.cancelled() and self._drain_task.exception() is not None:
            self._drain_task = _task(self._drain())
        interrupted = False
        with anyio.CancelScope(shield=True):
            while not self._drain_task.done():
                try:
                    await asyncio.shield(self._drain_task)
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:
                    break
        self._drain_task.result()
        if interrupted:
            raise asyncio.CancelledError
