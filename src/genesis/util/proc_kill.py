"""Process-group kill helpers for subprocess launchers.

Genesis spawns several external LAUNCHERS (``codex``, ``claude``, ``git`` and
its ssh/credential helpers, arbitrary step commands) that fork their own
children. Killing only the direct child on timeout orphans the tree — the
children reparent to pid 1 and keep running/spending until reboot. The safe
pattern (hardened across three Codex review rounds on PR #1409):

- Spawn with ``start_new_session=True`` — setsid is done async-signal-safely
  inside the C subprocess helper. NEVER ``preexec_fn``: arbitrary post-fork
  Python can deadlock in a multi-threaded parent (the genesis-server). The
  child then LEADS its own session/process group, so its pgid == its pid.
- On timeout, signal ``proc.pid`` AS the pgid directly. Never derive it via
  ``os.getpgid(proc.pid)``: once the leader exits and is reaped (asyncio's
  child watcher waitpid()s promptly), getpgid raises ProcessLookupError even
  while descendants keep the group alive — the kernel reserves the pgid until
  the LAST member dies, so ``killpg(pid)`` still reaps the survivors.
- Bound the post-kill reap: a paused pipe transport can stall an unbounded
  ``wait()``, turning the timeout recovery itself into a hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

logger = logging.getLogger(__name__)

DEFAULT_REAP_TIMEOUT_S = 30.0


class ProcessCleanupUncertain(RuntimeError):
    """Managed supervisor must retain ownership; process exit is unconfirmed."""


def kill_process_group(proc, sig: signal.Signals = signal.SIGKILL) -> None:
    """Signal the process group led by ``proc`` (Popen or asyncio Process).

    ``proc`` must have been spawned with ``start_new_session=True`` so its pid
    is its pgid. Guards: a non-int or <=1 pid never reaches killpg —
    ``killpg(1, sig)`` equals ``kill(-1, sig)``: every process we own — and
    falls back to the direct ``proc.kill()``. A vanished group
    (ProcessLookupError) is success; any other OS refusal falls back to the
    direct kill.
    """
    pgid = getattr(proc, "pid", None)
    if not isinstance(pgid, int) or pgid <= 1:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # whole group already gone — success, silent by design
    except OSError:
        # A genuine OS refusal degrades to the known-bad direct kill (the
        # tree can leak) — that must be VISIBLE, unlike the vanished-group
        # case above.
        logger.warning(
            "killpg(%s) failed; falling back to direct kill (children may leak)",
            pgid,
            exc_info=True,
        )
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def process_group_alive(proc) -> bool:
    """True iff the process group led by ``proc`` still has a live member.

    Signal-0 probe on the pid-as-pgid. Used to decide kill ESCALATION after a
    graceful stop: the LEADER exiting (returncode set) does not mean the group
    is gone — a descendant can survive it, and gating escalation on the
    leader's returncode alone leaks that descendant. Non-int/<=1 pids and any
    probe refusal (vanished, or recycled to a foreign uid) read as not-alive —
    there is nothing further we could kill in those cases anyway.
    """
    pgid = getattr(proc, "pid", None)
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


async def reap_bounded(proc, timeout_s: float | None = None) -> None:
    """Await ``proc.wait()`` with a hard bound; never raises — except a
    CancelledError arriving MID-reap, which propagates (suppress(Exception)
    excludes BaseException). That is safe by construction: every call site
    runs the synchronous ``kill_process_group`` first, so a second cancel
    abandons only the wait, never the kill, and asyncio's child watcher
    still reaps the leader.

    ``timeout_s=None`` resolves ``DEFAULT_REAP_TIMEOUT_S`` at call time (so
    tests can shrink it via the module attribute).
    """
    if timeout_s is None:
        timeout_s = DEFAULT_REAP_TIMEOUT_S
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)


async def confirm_process_exit(
    proc,
    *,
    graceful: bool = False,
    escalation_grace_s: float = 0.0,
    confirm_timeout_s: float | None = None,
) -> None:
    """Opt-in strong drain: refusals are unknown, never proof of group exit.

    A managed receipt holds capacity while this waits. Shield the cleanup task
    against repeated cancellation; deliver cancellation after confirmation.

    ``graceful`` is for a child already asked to stop (SIGTERM after its
    result): it first gets the ordinary bounded reap, and a surviving group
    gets ``escalation_grace_s`` to flush, before the group kill and the
    confirmation that follows it.

    Absence is confirmed for at most ``confirm_timeout_s`` after the group kill
    (``None`` resolves ``DEFAULT_REAP_TIMEOUT_S`` at call time, the same bound
    as the ordinary reap). Past it the group is reported as uncertain rather
    than watched forever; the caller keeps holding what it owns either way.
    """
    if confirm_timeout_s is None:
        confirm_timeout_s = DEFAULT_REAP_TIMEOUT_S

    async def drain():
        if graceful:
            await reap_bounded(proc)
            if process_group_alive(proc):
                await asyncio.sleep(escalation_grace_s)
        with contextlib.suppress(OSError):
            kill_process_group(proc)
        loop = asyncio.get_running_loop()
        give_up = loop.time() + confirm_timeout_s
        while True:
            gone = False
            pgid = getattr(proc, "pid", None)
            if isinstance(pgid, int) and pgid > 1:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    gone = True
                except OSError:
                    pass  # Includes PermissionError: liveness remains unknown.
            if gone and proc.returncode is not None:
                return
            if loop.time() >= give_up:
                raise TimeoutError("process group absence unconfirmed")
            await asyncio.sleep(0.1)

    cleanup = asyncio.create_task(drain())
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break  # Classify below; never infer exit from a failed drain.
    if cleanup.cancelled() or cleanup.exception() is not None:
        raise ProcessCleanupUncertain("Process cleanup is unconfirmed") from None
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


def _raised_by_popen(exc: BaseException) -> bool:
    """True when ``exc`` came out of ``subprocess.Popen._execute_child``.

    That path raises only after it has waited for any child it forked (exec
    failure, fork failure), so no process from this launch survives it.
    """
    if not isinstance(exc, OSError):
        return False
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if code.co_name == "_execute_child" and code.co_filename.endswith("subprocess.py"):
            return True
        tb = tb.tb_next
    return False


async def spawn_owned(*args, **kwargs):
    """Retain a managed subprocess launch until its process handle is known.

    Cancelling CPython's creation coroutine kills only the transport's leader.
    Shield it, then drain the group before delivering the caller's cancel.
    """
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    interrupted = False
    try:
        while not launch.done():
            try:
                await asyncio.shield(launch)
            except asyncio.CancelledError:
                interrupted = True
        if launch.cancelled():
            raise ProcessCleanupUncertain("Process creation is unconfirmed")
        proc = launch.result()
    except Exception as exc:
        if _raised_by_popen(exc):
            # Popen itself failed (exec error, fork failure): CPython reaped
            # any child it forked before raising, so nothing is left to own.
            # An ordinary failure, not an uncertain one.
            raise
        # CPython may raise while connecting pipes AFTER Popen starts a child;
        # its transport closes only the leader. No handle means no proof that
        # startup descendants exited, including when the caller was cancelled.
        # Transfer uncertainty to the registered managed supervisor, never to
        # ordinary/nonstreaming callers (they do not use this opt-in helper).
        raise ProcessCleanupUncertain("Process creation is unconfirmed") from None
    if interrupted:
        await confirm_process_exit(proc)
        raise asyncio.CancelledError
    return proc
