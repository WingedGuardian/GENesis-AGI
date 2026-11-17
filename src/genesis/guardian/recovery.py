"""Recovery engine — HOST-SIDE. Executes recovery actions with verification.

Recovery actions in escalation order:
1. RESTART_SERVICES  — start or restart genesis-server, judged by its new start
2. IO_TRIAGE         — kill top I/O consumer (one per cycle)
3. RESOURCE_CLEAR    — clear /tmp, reclaim page cache, restart
4. REVERT_CODE       — git stash && git revert HEAD, restart; on `live`, drop the
                       newest candidate with the engine's own drop, confirm it,
                       then restart (never a revert there)
5. RESTART_CONTAINER — incus restart genesis
6. SNAPSHOT_ROLLBACK — incus snapshot restore genesis {last_healthy}
7. ESCALATE          — alert user, stop automated recovery

Each step: pre-alert → pre-snapshot → execute → wait → verify → post-alert.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction
from genesis.guardian.health_signals import collect_all_signals
from genesis.guardian.live_drop import (
    HALF_DONE_DROP,
    WAITING_ON_LOCK,
    drop_target,
    live_candidates,
    update_lock_holder,
)
from genesis.guardian.live_drop import shown as _shown
from genesis.guardian.snapshots import PRE_RECOVERY_LABEL, SnapshotManager
from genesis.guardian.state_machine import ConfirmationStateMachine

logger = logging.getLogger(__name__)


# The branch itself, then the shared predicate (#3013) run from the checkout.
_HEAD_PROBE = "git -C ~/genesis symbolic-ref -q HEAD"
_LIVE_PROBE = "python3 -I -S ~/genesis/scripts/lib/live_checkout.py ~/genesis"


#: genesis-server's start: polled every _POLL_S for up to _START_WAIT_S (a stop,
#: TimeoutStartSec and RestartSec, 90 + 90 + 10 s at the unit's values).
_START_WAIT_S = 200
_POLL_S = 5
_UNREAD_MAX = 3  # consecutive reads that fail: the container does not answer
_SHOW = (
    "systemctl --user show genesis-server"
    " -p ActiveState -p SubState -p InvocationID -p Result"
)
#: The boot repair's newest line in genesis-server's journal (#3492 D2).
_REPAIR_LINE = (
    "journalctl --user -u genesis-server --since -15min -n 200 --no-pager -o cat"
    " | grep -F 'live-move repair:' | tail -n 1"
)


async def _server_unit(container: str) -> dict[str, str] | None:
    """genesis-server's ActiveState, SubState and InvocationID, or None when
    they cannot be read (a login shell may print lines first: key=value only)."""
    rc, lines = await _in_container(container, _SHOW)
    if rc != 0:
        return None
    vals = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    return vals if "ActiveState" in vals else None


async def _in_container(
    container: str, cmd: str, timeout: float = 30.0,
) -> tuple[int, list[str]]:
    rc, stdout, _ = await _run_subprocess(
        "incus", "exec", container, "--", "su", "-", "ubuntu", "-c", cmd, timeout=timeout,
    )
    return rc, stdout.strip().splitlines()  # a login shell may print first


#: drop waits up to _DROP_WAIT_S for update.lock; the client gives up later.
_DROP_WAIT_S = 120
_DROP_TIMEOUT_S = 240.0
#: After a client timeout the drop may still run in the container (incus exec
#: does not stop it): poll until the engine shows it gone AND update.lock is
#: free, or give up.
_DROP_POLL_S = 10
_DROP_POLL_MAX_S = 180


async def container_on_live(container: str) -> bool:
    """False only when the container checkout is shown NOT to be on `live`.

    Two signals; either one saying live, or unclear, bans. Git's own HEAD: on
    `refs/heads/live` it is live whatever the manifest says (a missing manifest
    makes the predicate answer `other`, and its copy is the candidate's code);
    only another branch, or a detached HEAD (exit 1, no output), passes on. Then
    the predicate, whose word must be `other` with exit 1. A failed git, a broken
    copy of the predicate and an exec that never reported all count as live.
    """
    rc, head = await _in_container(container, _HEAD_PROBE)
    on_branch = rc == 0 and head[-1:] != [] and head[-1].startswith("refs/heads/")
    if head[-1:] == ["refs/heads/live"] or not (on_branch or (rc == 1 and not head)):
        return True
    rc, word = await _in_container(container, _LIVE_PROBE)
    return not (rc == 1 and word[-1:] == ["other"])


@dataclass(frozen=True)
class RecoveryResult:
    """Result of a recovery attempt."""

    action: RecoveryAction
    success: bool
    detail: str
    duration_s: float


class RecoveryEngine:
    """Execute recovery actions with pre/post verification."""

    def __init__(
        self,
        config: GuardianConfig,
        state_machine: ConfirmationStateMachine,
        snapshots: SnapshotManager,
        dispatcher: AlertDispatcher,
    ) -> None:
        self._config = config
        self._sm = state_machine
        self._snapshots = snapshots
        self._dispatcher = dispatcher

    async def execute(self, diagnosis: DiagnosisResult) -> RecoveryResult:
        """Execute the recommended recovery action."""
        action = diagnosis.recommended_action
        t0 = datetime.now(UTC)

        if action == RecoveryAction.ESCALATE:
            return await self._escalate(diagnosis)

        # Check exponential backoff before proceeding
        backoff_s = self._sm.recovery_backoff_remaining_s(action.value)
        if backoff_s > 0:
            attempts = (
                self._sm.state.io_triage_attempts
                if action == RecoveryAction.IO_TRIAGE
                else self._sm.state.recovery_attempts
            )
            logger.warning(
                "Recovery backoff: %.0fs remaining before attempt %d — deferring",
                backoff_s, attempts + 1,
            )
            return RecoveryResult(
                action=action,
                success=False,
                detail=f"Recovery deferred: {backoff_s:.0f}s backoff remaining",
                duration_s=0.0,
            )

        # Pre-recovery alert
        await self._dispatcher.send(Alert(
            severity=AlertSeverity.CRITICAL,
            title=f"Attempting recovery: {action.value}",
            body=f"Cause: {diagnosis.likely_cause}\n"
                 f"Confidence: {diagnosis.confidence_pct}%",
            likely_cause=diagnosis.likely_cause,
            proposed_action=action.value,
        ))

        # Mark state as recovering
        self._sm.set_recovering()

        # Pre-recovery snapshot (gated on config flag and disk space)
        snap_history = self._sm.state.snapshot_size_history
        if (
            action != RecoveryAction.SNAPSHOT_ROLLBACK
            and self._config.snapshots.take_pre_recovery
            and await self._snapshots.safe_to_snapshot(snap_history)
        ):
            snap_name = await self._snapshots.take(
                label=PRE_RECOVERY_LABEL,
                snapshot_size_history=snap_history,
            )
            if snap_name:
                logger.info("Pre-recovery snapshot: %s", snap_name)

        # Execute the action
        waiting_before = self._sm.state.waiting_on_update_lock
        try:
            success, detail = await self._execute_action(action, diagnosis)
        except Exception as exc:
            logger.error("Recovery action %s failed: %s", action, exc, exc_info=True)
            success = False
            detail = str(exc)

        if not success and not waiting_before and self._sm.state.waiting_on_update_lock:
            # Held back by update.lock on `live` (#2978): nothing ran, so it is
            # no attempt and costs no budget or backoff; the episode resumes
            # once the lock is free. Back to confirmed-dead, where the wait is
            # probed each tick (set_recovering above left it recovering).
            self._sm.set_confirmed_dead()
            await self._dispatcher.send(Alert(
                severity=AlertSeverity.WARNING,
                title=f"Recovery deferred: {action.value}",
                body=f"{detail}\nThe Guardian checks update.lock every tick.",
            ))
            return RecoveryResult(
                action=action, success=False, detail=detail,
                duration_s=(datetime.now(UTC) - t0).total_seconds(),
            )

        # Record attempt timestamp for backoff tracking
        self._sm.record_recovery_attempt(action.value)

        duration = (datetime.now(UTC) - t0).total_seconds()

        if success:
            # Wait for services to stabilize
            await asyncio.sleep(self._config.recovery.verification_delay_s)

            # Verify recovery via health probes
            snapshot = await collect_all_signals(self._config)
            if snapshot.all_alive:
                self._sm.set_recovered()
                self._sm.clear_down_alert_sent()  # GUARD-R2-01: episode over
                await self._dispatcher.send(Alert(
                    severity=AlertSeverity.INFO,
                    title=f"Recovery successful: {action.value}",
                    body=f"Genesis is back online after {duration:.0f}s.",
                    duration_s=duration,
                ))
            else:
                # Recovery action succeeded but system still unhealthy
                failed = [s.name for s in snapshot.failed_signals]
                detail = f"Action completed but probes still failing: {', '.join(failed)}"
                success = False
                self._sm.set_confirmed_dead()
                logger.warning("Recovery verification failed: %s", detail)
        else:
            self._sm.set_confirmed_dead()
            await self._dispatcher.send(Alert(
                severity=AlertSeverity.CRITICAL,
                title=f"Recovery failed: {action.value}",
                body=detail,
                duration_s=duration,
            ))

        return RecoveryResult(
            action=action,
            success=success,
            detail=detail,
            duration_s=duration,
        )

    async def _execute_action(
        self, action: RecoveryAction, diagnosis: DiagnosisResult | None = None,
    ) -> tuple[bool, str]:
        """Execute a single recovery action. Returns (success, detail)."""
        container = self._config.container_name

        if action == RecoveryAction.RESTART_SERVICES:
            return await self._restart_services(container)
        elif action == RecoveryAction.IO_TRIAGE:
            return await self._io_triage(container)
        elif action == RecoveryAction.RESOURCE_CLEAR:
            return await self._resource_clear(container)
        elif action == RecoveryAction.REVERT_CODE:
            return await self._revert_code(
                container, diagnosis.drop_candidate if diagnosis else None,
            )
        elif action == RecoveryAction.RESTART_CONTAINER:
            return await self._restart_container(container)
        elif action == RecoveryAction.SNAPSHOT_ROLLBACK:
            return await self._snapshot_rollback()
        else:
            return False, f"Unknown action: {action}"

    async def _restart_services(self, container: str) -> tuple[bool, str]:
        """Restart genesis-server inside the container, judged by the unit itself.

        A start in progress is left to finish: the boot repair heals the
        checkout in the unit's start step, and a restart would SIGTERM it
        (#3492 D2). A stopped or failed unit is started, a running one
        restarted, both without blocking; then the unit is polled until a NEW
        invocation is active (the old process can still read active just after
        a --no-block restart), or it failed, or _START_WAIT_S passed. The bound
        covers a stop (90 s), a start step (90 s) and RestartSec (10 s)."""
        before = await _server_unit(container)
        state = (before or {}).get("ActiveState")
        if state == "activating":
            cmd, old = None, None
        else:
            verb = "start" if state in ("failed", "inactive") else "restart"
            cmd, old = f"systemctl --user {verb} --no-block genesis-server", before
        if cmd:
            rc, _out, stderr = await _run_subprocess(
                "incus", "exec", container, "--", "su", "-", "ubuntu", "-c", cmd, timeout=30.0,
            )
            # A client timeout is not the result: the unit's own state decides.
            if rc != 0 and not (rc == -1 and stderr == "timeout"):
                return False, f"systemctl {cmd.split()[2]} failed: {stderr}"
        old_id = (old or {}).get("InvocationID")
        last, unread = before or {}, 0
        # A clock bound (each read can take its 30 s timeout) and a poll bound
        # (the clock alone never ends where sleep is patched out).
        end = asyncio.get_running_loop().time() + _START_WAIT_S
        for n in range(_START_WAIT_S // _POLL_S + 1):
            if n:
                if asyncio.get_running_loop().time() >= end:
                    break
                await asyncio.sleep(_POLL_S)
            cur = await _server_unit(container)
            if cur is None:
                unread += 1
                if unread >= _UNREAD_MAX:
                    last = {"ActiveState": "unreadable", "SubState": "unreadable"}
                    break
                continue
            unread, last = 0, cur
            if cur.get("Result") == "start-limit-hit" and cur.get("ActiveState") == "failed":
                break  # systemd refuses every start until the limit resets
            if old_id and cur.get("InvocationID") == old_id:
                continue  # the old process, or the start has not run yet
            if cur.get("ActiveState") == "active":
                return True, "genesis-server started"
            if cur.get("ActiveState") == "failed":
                break
        _rc, lines = await _in_container(container, _REPAIR_LINE)
        lines = [ln for ln in lines if "live-move repair:" in ln]  # not a login banner
        why = f"; its start step's last line: {lines[-1][:400]}" if lines else ""
        shown = f"{last.get('ActiveState', 'unknown')}/{last.get('SubState', 'unknown')}"
        if last.get("Result") == "start-limit-hit":
            shown += ", start limit hit: systemd refuses starts until it resets"
        return False, f"genesis-server did not come up ({shown}){why}"

    async def _io_triage(self, container: str) -> tuple[bool, str]:
        """Kill the top I/O consumer. One process per cycle — reassess after.

        Checks PSI trend before acting: if pressure is already dropping,
        stands down and lets the system recover naturally.
        Never touches io.max (host safety boundary).
        """
        import asyncio

        from genesis.guardian.cgroup_ops import (
            find_top_io_pids,
            find_top_io_pids_rate,
            kill_pid,
            read_io_pressure,
        )

        # 1. Collect diagnostics — rate-based ranking (500ms delta sample)
        #    identifies the actual current I/O offender, not just the
        #    process with the highest cumulative lifetime total.
        top_pids = await asyncio.to_thread(find_top_io_pids_rate, container, 5)
        if not top_pids:
            # Fallback to cumulative if rate sampling fails (all PIDs gone)
            top_pids = find_top_io_pids(container, top_n=5)
            if not top_pids:
                return False, "No I/O consuming processes found in container cgroup"

        # Log all candidates for observability
        for entry in top_pids:
            if "total_rate" in entry:
                logger.info(
                    "IO_TRIAGE candidate: PID %d (%s) "
                    "rate=%.0f bytes/s (cumulative: read=%d write=%d)",
                    entry["pid"], entry["comm"], entry["total_rate"],
                    entry.get("read_bytes_cumulative", 0),
                    entry.get("write_bytes_cumulative", 0),
                )
            else:
                logger.info(
                    "IO_TRIAGE candidate (cumulative fallback): PID %d (%s) "
                    "read=%d write=%d total=%d bytes",
                    entry["pid"], entry["comm"],
                    entry["read_bytes"], entry["write_bytes"],
                    entry["total_bytes"],
                )

        # 2. Assess PSI trend — is pressure accelerating or recovering?
        pressure = read_io_pressure(container)
        if pressure:
            avg10 = pressure.get("full_avg10", 0)
            avg60 = pressure.get("full_avg60", 0)
            if avg10 < avg60:
                # Pressure is dropping — stand down
                return True, (
                    f"I/O pressure recovering (avg10={avg10:.1f}%, "
                    f"avg60={avg60:.1f}%) — standing down"
                )

        # 3. Kill top consumer only (one per cycle)
        target = top_pids[0]
        killed = await kill_pid(target["pid"], container=container)
        if not killed:
            return False, (
                f"Failed to kill PID {target['pid']} ({target['comm']})"
            )

        # Report rate if available, else cumulative
        if "total_rate" in target:
            io_detail = f"rate={target['total_rate']:.0f} bytes/s"
        else:
            io_detail = f"total_bytes={target['total_bytes']}"

        return True, (
            f"Killed top I/O consumer: PID {target['pid']} "
            f"({target['comm']}) {io_detail}"
        )

    async def _resource_clear(self, container: str) -> tuple[bool, str]:
        """Clear /tmp and reclaim page cache, then restart services."""
        # Clear /tmp (preserve essential sockets)
        rc, _, stderr = await _run_subprocess(
            "incus", "exec", container, "--",
            "find", "/tmp", "-type", "f",  # noqa: S108 - hygiene scan of host /tmp, not temp creation
            "-not", "-name", "*.sock",
            "-mmin", "+5", "-delete",
            timeout=30.0,
        )
        if rc != 0:
            logger.warning("tmp cleanup had errors: %s", stderr)

        # Reclaim page cache
        rc, _, _ = await _run_subprocess(
            "incus", "exec", container, "--",
            "bash", "-c", "sync && echo 1 > /proc/sys/vm/drop_caches",
            timeout=10.0,
        )

        # Restart services
        return await self._restart_services(container)

    async def _revert_code(
        self, container: str, drop: tuple[str, str, str] | None = None,
    ) -> tuple[bool, str]:
        """Off `live`: stash uncommitted changes and revert the last commit, then
        restart. On `live`, with the candidate the approval named: drop it."""
        # What was approved on `live` is the drop of that candidate (check.py
        # sets it before Gate 2), so it never falls through to a revert, even if
        # the checkout has left `live` since: the drop re-checks and refuses.
        if drop is not None:
            return await self._drop_candidate(container, *drop)
        # Never a revert on `live` (#3492 D3). The stash would push a half-written
        # tree, or a candidate's working files, onto the stash stack every session
        # shares, where any session's later `stash pop` lays it over another tree;
        # the revert would commit onto the integration branch the deploy engine
        # rebuilds. check.py proposes a drop or RESTART_CONTAINER there before
        # the approval; this refusal is the backstop for a branch that moved
        # since. It fires after execute() has taken its snapshot, sent its alert
        # and recorded the attempt; acceptable, since it only fires in the window
        # between the two probes.
        if await container_on_live(container):
            return False, (
                "REVERT_CODE refused: the container checkout is on `live` (or could "
                "not be shown not to be), where a code revert never runs, and no "
                "candidate drop was approved. A container restart (RESTART_CONTAINER) "
                "lets a boot repair run where one is installed; a bad candidate is "
                "removed with `scripts/deploy_candidates drop <branch>`"
            )
        # Stash any uncommitted work. git revert itself refuses to overwrite
        # dirty files, so this guard is not about data loss: a failed stash
        # leaves uncommitted code live, a revert commit cannot fix a fault that
        # lives there, and an unfinished stash may still hold the index.
        rc, _, stderr = await _run_subprocess(
            "incus", "exec", container, "--",
            "su", "-", "ubuntu", "-c",
            "cd ~/genesis && git stash",
            timeout=30.0,
        )
        if rc == -1:
            # _run_subprocess returns -1 for a timeout or exec error: the stash
            # did not report, so whether it ran is unknown.
            return False, (
                f"git stash did not report ({stderr}); not reverting. "
                "Check `git stash list` and the working tree before retrying"
            )
        if rc != 0:
            return False, (
                f"git stash (or the exec around it) failed ({stderr}); not "
                "reverting. Check `git stash list` and the working tree"
            )

        # Revert the last commit
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", container, "--",
            "su", "-", "ubuntu", "-c",
            "cd ~/genesis && git revert --no-edit HEAD",
            timeout=30.0,
        )
        if rc != 0:
            return False, f"git revert failed: {stderr}"

        # Restart services after code change
        svc_ok, svc_detail = await self._restart_services(container)
        return svc_ok, f"Code reverted, {svc_detail}"

    async def _drop_candidate(
        self, container: str, branch: str, head: str, tip: str,
    ) -> tuple[bool, str]:
        """Take the approved candidate out of `live` with the engine's own drop,
        confirm through the engine that it is gone, then restart genesis-server.
        Nothing restarts unless the drop is confirmed and update.lock is free:
        a restart while a drop, or a rebuild queued behind it, still runs would
        start the server on a tree that is moving (owner rulings, #2978)."""
        data = await live_candidates(container)
        now, why = drop_target(data) if data is not None else (None, "the engine's list could not be read")
        if now != (branch, head, tip):
            return False, (
                f"drop of {branch} refused before it ran: the candidate set changed since "
                f"approval ({why or 'the newest candidate or `live` moved'}). Nothing was "
                "changed and nothing restarted"
            )
        cmd = (
            f"cd ~/genesis && scripts/deploy_candidates drop {shlex.quote(branch)} "
            f"--expect-branch live --expect-head {tip} --wait {_DROP_WAIT_S}"
        )
        rc, stdout, stderr = await _run_subprocess(
            "incus", "exec", container, "--", "su", "-", "ubuntu", "-c", cmd,
            timeout=_DROP_TIMEOUT_S,
        )
        notes = [_shown(ln, 300) for ln in stdout.splitlines() if "NOTE:" in ln][:3]
        if rc == -1 and stderr == "timeout":
            # The command may still be running in the container (incus exec does
            # not stop it): wait for update.lock, then judge the engine's view.
            free = await self._wait_for_lock(container)
            gone, seen = await self._drop_confirmed(container, branch)
            if free is not True:
                self._sm.mark_waiting_on_update_lock()
                return False, (
                    f"drop of {branch} had not finished after {_DROP_TIMEOUT_S + _DROP_POLL_MAX_S:.0f} s "
                    "(update.lock is held, or could not be read); nothing restarted. The "
                    "Guardian diagnoses again once the lock is free"
                )
            if not gone:
                return False, (
                    f"drop of {branch} did not report in {_DROP_TIMEOUT_S:.0f} s and the lock "
                    f"is free, but the engine does not show it gone ({seen}); nothing restarted"
                )
        elif rc == 200:
            self._sm.mark_waiting_on_update_lock()
            return False, (
                f"drop of {branch} did not run: update.lock was held for {_DROP_WAIT_S} s "
                "(a deploy, rebuild or validation hold). Nothing was dropped and nothing "
                "restarted; the Guardian diagnoses again once the lock is free"
            )
        elif rc != 0:
            err = [ln for ln in stderr.splitlines() if ln.strip()]
            text = " … ".join(_shown(ln, 300) for ln in dict.fromkeys(err[:1] + err[-1:]))
            return False, f"drop of {branch} failed ({text or f'exit {rc}'}); nothing restarted"
        else:
            gone, seen = await self._drop_confirmed(container, branch)
            if not gone:
                return False, (
                    f"drop of {branch} exited 0 but the engine does not show it gone ({seen}); "
                    "nothing restarted"
                )
            # A rebuild queued behind the drop can take update.lock at once:
            # restart only once it is free again.
            if await self._wait_for_lock(container) is not True:
                self._sm.mark_waiting_on_update_lock()
                return False, (
                    f"dropped {branch} from `live`, but update.lock is held on `live` (or could "
                    "not be read); nothing restarted. The Guardian diagnoses again once the "
                    "lock is free"
                )
            # Judged again after the wait: a rebuild that held the lock meanwhile
            # can have moved the checkout or left it half-written.
            gone, seen = await self._drop_confirmed(container, branch)
            if not gone:
                return False, (
                    f"dropped {branch} from `live`, but after waiting for update.lock the "
                    f"engine reports: {seen}; nothing restarted"
                )
        ok, detail = await self._restart_services(container)
        extra = f" ({' '.join(notes)})" if notes else ""
        return ok, f"dropped {branch} ({head[:12]}) from `live`{extra}; {detail}"

    async def _drop_confirmed(self, container: str, branch: str) -> tuple[bool, str]:
        """Whether the engine shows ``branch`` gone from the manifest and from
        `live`, with the checkout on `live` and nothing that would refuse a move
        (a half-moved tree reads dirty there), and what it saw otherwise."""
        data = await live_candidates(container)
        if data is None:
            return False, "the engine's list could not be read"
        holds = data["live"].get("holds") or []
        if any(r["branch"] == branch for r in data["listed"]):
            return False, f"the manifest still lists {branch}"
        if any(h["branch"] == branch for h in holds) or data["live"].get("holds") is None:
            return False, f"`live` still holds {branch}, or what it holds could not be read"
        if (data.get("checkout") or {}).get("branch_ref") != "refs/heads/live":
            return False, "the checkout is not on `live`"
        if data["move_refusals"]:
            return False, _shown(data["move_refusals"][0])
        return True, ""

    async def _wait_for_lock(self, container: str) -> bool | None:
        """Poll update.lock until it is free (True), for up to _DROP_POLL_MAX_S:
        False when still held at the end, None when the last probe could not
        say. A clock bound and a poll bound, as in _restart_services."""
        deadline = asyncio.get_running_loop().time() + _DROP_POLL_MAX_S
        held: bool | None = None
        for n in range(_DROP_POLL_MAX_S // _DROP_POLL_S + 1):
            if n:
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(_DROP_POLL_S)
            held, _who = await update_lock_holder(container)
            if held is False:
                return True
        return None if held is None else False

    async def _lock_blocks_restart(self, container: str) -> str:
        """Why a restart must not run now, or "": update.lock is held on `live`
        (a deploy, rebuild, drop or validation hold that took it after Gate 2).
        The lock is read first and the branch only when it is held; a probe that
        cannot say does not block (the approval stands)."""
        held, who = await update_lock_holder(container)
        if not held or not await container_on_live(container):
            return ""
        return "update.lock is held on `live`" + (f" ({who})" if who else "")

    async def _restart_container(self, container: str) -> tuple[bool, str]:
        """Restart the entire container (or start it, if stopped).

        `incus restart` FAILS on a stopped instance — the 2026-07-04 outage
        (unclean host reboot left the container stopped) made this recovery
        action a guaranteed no-op exactly when it was needed. On restart
        failure, fall back to `incus start`: if the container was merely
        stopped, start brings it back; if it was running (restart failed for
        another reason), start fails too and the original error is reported.
        On `live` it never runs while update.lock is held: the approval may be
        hours old, and a deploy may have taken the lock since (#2978).
        """
        blocked = await self._lock_blocks_restart(container)
        if blocked:
            self._sm.mark_waiting_on_update_lock()
            return False, (
                f"container restart refused: {blocked}; nothing restarted. The Guardian "
                "diagnoses again once the lock is free"
            )
        rc, stdout, stderr = await _run_subprocess(
            "incus", "restart", container, "--timeout", "30",
            timeout=60.0,
        )
        if rc == 0:
            return True, "Container restarted"

        start_rc, _, start_err = await _run_subprocess(
            "incus", "start", container,
            timeout=60.0,
        )
        if start_rc == 0:
            return True, "Container started (was stopped — restart cannot start a stopped instance)"
        return False, f"incus restart failed: {stderr}"

    async def _snapshot_rollback(self) -> tuple[bool, str]:
        """Rollback to the last healthy snapshot.

        If the restore fails and NEWER guardian snapshots exist, delete them
        and retry once: some storage drivers (documented: ZFS) refuse to
        restore a non-latest snapshot. The newer snapshots are guardian-made
        captures of the already-broken state (e.g. pre-recovery) — the healthy
        restore is last-resort, so sacrificing them is the right trade.
        """
        healthy = await self._snapshots.get_latest_healthy()
        if not healthy:
            return False, "No healthy snapshot available for rollback"

        ok = await self._snapshots.restore(healthy)
        if not ok:
            # list_snapshots() is newest-first by incus's created_at (never by
            # name string), and get_latest_healthy() reads the same order, so
            # everything listed before the healthy snapshot is newer.
            names = await self._snapshots.list_snapshots()
            newer = names[: names.index(healthy)] if healthy in names else []
            if newer:
                logger.warning(
                    "Restore of %s failed with newer snapshots present — "
                    "deleting %s and retrying once",
                    healthy, ", ".join(newer),
                )
                for name in newer:
                    await self._snapshots.delete(name)
                ok = await self._snapshots.restore(healthy)
        if not ok:
            return False, f"Failed to restore snapshot {healthy}"
        return True, f"Restored snapshot {healthy}"

    async def _escalate(self, diagnosis: DiagnosisResult) -> RecoveryResult:
        """Send escalation alert — no automated recovery."""
        attempts = len(diagnosis.actions_taken)
        actions_str = ", ".join(diagnosis.actions_taken) if diagnosis.actions_taken else "none"
        cc_note = ""
        if diagnosis.cc_failure_reason:
            cc_note = f"CC failure: {diagnosis.cc_failure_reason}\n"
        cname = self._config.container_name

        if diagnosis.escalation == WAITING_ON_LOCK:
            # Not a failed recovery: a run holds update.lock on `live`, and a
            # drop or restart now would cut into it (owner ruling, #2978).
            await self._dispatcher.send(Alert(
                severity=AlertSeverity.WARNING,
                title="Genesis down — waiting for a deploy to finish",
                body=(
                    f"Cause: {diagnosis.likely_cause}\n"
                    "Nothing was restarted or dropped. The Guardian checks update.lock "
                    "every tick and diagnoses again once it is free, asking your approval "
                    "for what it then proposes. If the lock is still held after 2 h, the "
                    "run holding it is likely hung: you will hear again then, and every "
                    "2 h after."
                ),
                likely_cause=diagnosis.likely_cause,
            ))
            return RecoveryResult(
                action=RecoveryAction.ESCALATE, success=True,
                detail="Waiting for update.lock", duration_s=0.0,
            )
        if diagnosis.escalation == HALF_DONE_DROP:
            await self._dispatcher.send(Alert(
                severity=AlertSeverity.EMERGENCY,
                title="Genesis down — a drop did not finish",
                body=(
                    f"Cause: {diagnosis.likely_cause}\n"
                    f"Finish it: incus exec {cname} -- su - ubuntu -c "
                    "'cd ~/genesis && scripts/deploy_candidates rebuild', then restart "
                    "genesis-server. A container restart alone starts the same code."
                ),
                likely_cause=diagnosis.likely_cause,
                failed_probes=diagnosis.evidence,
            ))
            return RecoveryResult(
                action=RecoveryAction.ESCALATE, success=True,
                detail="Escalated: a drop did not finish", duration_s=0.0,
            )

        await self._dispatcher.send(Alert(
            severity=AlertSeverity.EMERGENCY,
            title="Genesis down — all automated recovery failed",
            body=(
                f"Cause: {diagnosis.likely_cause}\n"
                f"Confidence: {diagnosis.confidence_pct}%\n"
                f"{cc_note}"
                f"Actions tried ({attempts}): {actions_str}\n\n"
                "IMMEDIATE ACTION REQUIRED:\n"
                "1. SSH to host VM\n"
                "2. Check Guardian log: ~/.local/state/genesis-guardian/guardian.log\n"
                f"3. Check container: incus exec {cname} -- "
                f"su - ubuntu -c 'journalctl --user -n 200'\n"
                f"4. Manual restart: incus exec {cname} -- "
                f"su - ubuntu -c 'systemctl --user restart genesis-server'"
            ),
            likely_cause=diagnosis.likely_cause,
            failed_probes=diagnosis.evidence,
        ))
        return RecoveryResult(
            action=RecoveryAction.ESCALATE,
            success=True,  # escalation itself always "succeeds"
            detail="Escalated to user",
            duration_s=0.0,
        )
