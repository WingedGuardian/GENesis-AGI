"""Finite original-owner control; children never inherit the indexing locks."""
from __future__ import annotations

import json
import os
import select
import shlex
import signal
import sqlite3
import subprocess
import time
from pathlib import Path


def control_environment(reserved, policy, unit):
    """Actual control environment plus the accepted destination admission inputs."""
    selected = unit.systemctl_env()
    selected.pop('CODE_INTEL_CHILD_REFUSAL_MARKER', None)
    selected.update(CODE_INTEL_CHILD_REQUIRE_WORKLOAD_SLICE='1' if reserved['route'] == 'workload' else '0',
        CODE_INTEL_CHILD_CAP_BYTES=str(reserved['cap_bytes']), CODE_INTEL_CHILD_SCOPE_UNIT=reserved['unit'],
        CODE_INTEL_CHILD_RESERVE_BYTES=str(policy['sibling_reserve_bytes']),
        CODE_INTEL_FILE_CACHE_RESERVE_BYTES=str(policy['cache_reserve_bytes']))
    return selected


def scope_command(snapshot, modules, ack_fd):
    """One protected Bash exec preserves the actual reporter child birth."""
    value, _ = modules['queued'].runtime_inputs(snapshot, modules)
    modules['queue']._native._integer(ack_fd, 'private ACK descriptor', positive=True)
    if ack_fd < 3:
        raise ValueError('startup ACK must use a private descriptor')
    reserved, policy = value['reservation'], value['policy']
    route = '--slice-inherit' if reserved['route'] == 'inherit' else '--slice=' + reserved['slice']
    argv = ['/usr/bin/systemd-run', '--user', '--scope', '--quiet', '--no-ask-password',
            '--expand-environment=no', route, '--unit=' + reserved['unit'],
            '-p', 'MemoryMax=' + str(reserved['cap_bytes']), '-p', 'MemorySwapMax=0',
            '-p', 'IOWeight=' + policy['io_weight'], '-p', 'CPUQuota=' + policy['cpu_quota'],
            '--', *snapshot.resume_command('reporter', str(ack_fd))]
    return ['/bin/bash', '--noprofile', '--norc', '-p', '-c', 'exec ' + shlex.join(argv)]


def monitor_command(snapshot, modules, child_pid):
    """Held pressure policy requests actions; only the original owner acts."""
    value, _ = modules['queued'].runtime_inputs(snapshot, modules)
    modules['queue']._native._integer(child_pid, 'owned foreground PID', positive=True)
    if child_pid <= 1:
        raise ValueError('monitor requires an owned workload child')
    policy, reserved = value['policy'], value['reservation']
    names = dict(interval='_WD_INTERVAL', warmup_interval='_WD_WARMUP_INTERVAL',
                 warmup_seconds='_WD_WARMUP_S', load_max='_WD_LOAD_MAX',
                 iowait_max='_WD_IOWAIT_MAX', bad_samples='_WD_BAD_SAMPLES',
                 continuous_pause_max='_WD_CONT_PAUSE_MAX')
    settings = {names[key]: raw for key, raw in policy['watchdog'].items()}
    settings.update(MODE=reserved['run_mode'], _WD_WALL_FAST=str(policy['wall_seconds']),
                    _WD_WALL_FULL=str(policy['wall_seconds']))
    program = '\n'.join(snapshot.buffers[name].decode('utf-8') for name in (
        'scripts/lib/proc_pressure.sh', 'scripts/lib/code_intel_scope.sh'))
    program += '\n' + '\n'.join(key + '=' + shlex.quote(raw) for key, raw in settings.items())
    program += r'''
_log() { printf '%s\n' "$*" >&2; }
_wd_cancelled() {
    if IFS= builtin read -r -t 0; then
        if LC_ALL=C IFS= builtin read -r -n 1 -d '' _WD_BYTE; then exit 125; fi
        return 0
    fi
    return 1
}
_wd_send() { _wd_cancelled && return 0; printf '%s\n' "$1" || exit 125; }
_wd_alive() { ! _wd_cancelled && builtin kill -0 "$1" 2>/dev/null; }
_wd_pause() { _wd_send freeze; }
_wd_resume() { _wd_send thaw; }
_wd_kill() {
    case "${_WD_STOP_REASON:-}" in
        wall|pressure_stop) _wd_send "$_WD_STOP_REASON" ;;
        *) exit 125 ;;
    esac
}
_wd_send ready
'''
    program += '_watchdog scope ' + shlex.quote(reserved['unit']) + ' ' + str(child_pid) + ' || exit "$?"\n'
    # Even a completed policy request waits for owner-requested retirement.
    # EOF retires after current samples/sleep; no signal trap reenters Bash.
    program += 'while ! _wd_cancelled; do sleep "$_WD_INTERVAL"; done\n'
    return ['/bin/bash', '--noprofile', '--norc', '-p', '-c', program]


class OwnerRecords:
    """Bound each atomic producer frame; decoding never performs journal I/O."""

    def __init__(self, owner):
        self.owner, self.pending, self.ready, self.ended = owner, bytearray(), False, False
        self.observed_ns = 0
        self.last_report = None

    def feed(self, raw):
        if self.ended or not isinstance(raw, bytes) or len(raw) > self.owner.proof._LIMIT:
            raise ValueError('invalid owner output chunk')
        self.pending.extend(raw)
        records = []
        while b'\n' in self.pending:
            line, _, tail = self.pending.partition(b'\n')
            if len(line) + 1 > self.owner.proof._LIMIT:
                raise ValueError('owner output frame exceeds atomic bound')
            self.pending = bytearray(tail)
            record = bytes(line).decode('utf-8')
            if record == 'ready':
                if self.ready:
                    raise ValueError('duplicate monitor readiness')
                self.ready = True
            elif record in ('freeze', 'thaw', 'wall', 'pressure_stop'):
                if not self.ready:
                    raise ValueError('monitor request precedes readiness')
            else:
                owner = self.owner
                if owner.startup is None:
                    raise ValueError('startup sample cannot establish an owner scope')
                gate = owner.modules['queued'].StartupGate(owner.queue, owner.proof, owner.context, owner.reserved)
                value = gate.observation(owner.startup['scope'], json.loads(record, object_pairs_hook=owner.proof._unique))
                if value['observed_ns'] < self.observed_ns:
                    raise ValueError('startup sample observation moved backwards')
                if self.last_report is not None:
                    owner.proof._complete(self.last_report, value['facts'])
                self.last_report = value['facts']
                self.observed_ns = value['observed_ns']
                owner._select_facts(value['facts'], value['observed_ns'])
                record = 'owned_oom' if owner.modules['queued'].sampled_owned_oom({'facts': value['facts']}) else 'sample'
            records.append(record)
        if len(self.pending) >= self.owner.proof._LIMIT:
            raise ValueError('owner output frame exceeds atomic bound')
        return records

    def eof(self):
        if self.pending or not self.ready:
            raise ValueError('owner output ended without complete monitor protocol')
        self.ended = True


class OwnedChild:
    """Retain one actual Popen birth and cache only its raw owned wait."""

    def __init__(self, child, proof):
        if not isinstance(child, subprocess.Popen) or child.returncode is not None:
            raise ValueError("physical owner requires its actual unreaped child")
        self.child, self.proof = child, proof
        self._result, self._lost_wait = None, False
        self._identity = self._read()

    def _read(self):
        process = Path('/proc') / str(self.child.pid)
        before = self.proof._process_state(process, self.child.pid)
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if (before[2] != os.getpid()
                or self.proof._process_state(process, self.child.pid) != before
                or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot):
            raise ValueError("original owned child birth or parent changed")
        return dict(pid=before[0], start_ticks=before[1], boot_id=boot)

    @property
    def identity(self):
        return dict(self._identity)

    def status(self):
        if self._lost_wait:
            raise ChildProcessError("original child wait is unavailable")
        if self._result is None:
            try:
                self._result = self.proof._payload_status(self.child)
            except ChildProcessError:
                self._lost_wait = True
                raise
        return self._result

    def terminate(self, *, group=False):
        """Cancel only this still-owned birth, optionally its own monitor group."""
        if self.status() is not None:
            return False
        if self._read() != self._identity:
            raise ValueError("original owned child birth changed")
        if group:
            if os.getpgid(self.child.pid) != self.child.pid or os.getsid(self.child.pid) != self.child.pid:
                raise ValueError("monitor does not own its isolated process group")
            os.killpg(self.child.pid, signal.SIGTERM)
        else:
            os.kill(self.child.pid, signal.SIGTERM)
        return True


class PhysicalOwner:
    """Original-scope control; journal publication follows required action."""

    def __init__(self, modules, context, reserved, probe, manager, *, control=None, clock=time.monotonic):
        self.modules, self.queue, self.proof = modules, modules['queue'], modules['proof']
        self.native = self.queue._native
        self.context = self.native.context(context, self.queue._legs, self.queue.marker_hash)
        self.reserved = json.loads(self.native.encode(self.native.reservation(reserved, self.context)))
        status = self.queue.execution_status(self.context)
        if (self.reserved['owner'] != self.queue._recovery.owner_identity()
                or status['launch_reservation'] != self.reserved
                or status['launch_permission'] != 'reserved'
                or status['scope_registration'] is not None or status['native_intent'] is not None):
            raise ValueError('physical owner requires its own fresh acknowledged reservation')
        self.probe, self.manager, self.clock = probe, manager, clock
        self.work_deadline = manager.deadline
        self._control = self._systemctl if control is None else control
        self.foreground = None
        self.startup = self.object_path = self.stop = self.final_deadline = None
        self.diagnostics = []
        self.launched = False
        self.first_facts, self.startup_error = None, False
        self.owner_facts = self.owner_observed_ns = None

    def run(self, snapshot):
        """Own both real children, release ACK once, then publish proved closure.

        Unknown child/channel/physical lifetime leaves the default-open journal
        obligation in place. An exit status alone never supplies cleanup proof.
        """
        if self.launched or signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            raise ValueError('owner cannot relaunch or use automatic child reaping')
        value, _ = self.modules['queued'].runtime_inputs(snapshot, self.modules)
        if value['context'] != self.context or value['reservation'] != self.reserved:
            raise ValueError('physical runtime differs from acknowledged reservation')
        self.launched = True
        ack_read, ack_write = os.pipe()
        output_read, output_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        monitor_read = monitor_write = None
        monitor = None
        records = OwnerRecords(self)
        ended = acknowledged = failed = stopping = monitor_stopped = False
        cancelled = []
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        for sig in previous:
            signal.signal(sig, lambda number, frame: cancelled.append(number))
        cleanup = None
        try:
            with snapshot.sealed() as capsule:
                child = subprocess.Popen(scope_command(snapshot, self.modules, ack_read), stdin=capsule,
                    stdout=output_write, close_fds=True, pass_fds=(ack_read,),
                    env=control_environment(self.reserved, value['policy'], self.modules['unit']))
            os.close(ack_read)
            ack_read = None
            self.foreground = OwnedChild(child, self.proof)
            monitor_read, monitor_write = os.pipe()  # Non-inheritable; owner alone keeps its writer.
            monitor_process = subprocess.Popen(monitor_command(snapshot, self.modules, child.pid),
                stdin=monitor_read, stdout=output_write, close_fds=True,
                start_new_session=True, env=self.modules['unit'].systemctl_env())
            os.close(monitor_read)
            monitor_read = None
            monitor = OwnedChild(monitor_process, self.proof)
            os.close(output_write)
            output_write = None
            while True:
                foreground_rc, monitor_rc = self.foreground.status(), monitor.status()
                if monitor_rc is not None and not monitor_stopped:
                    failed = True  # Status0 cannot invent requested retirement.
                if self.startup is None and foreground_rc is None and not stopping:
                    try:
                        ack = self.early()
                    except (OSError, ValueError, subprocess.SubprocessError):
                        # The direct child may not have entered its scope yet.
                        # A retained first sample (including OOM) is not retried.
                        if self.startup is not None or self.startup_error:
                            failed = True
                if (cancelled or failed or monitor_rc is not None and not monitor_stopped
                        or self.clock() >= self.work_deadline) and not stopping:
                    reason = 'wall' if self.clock() >= self.work_deadline else 'cancellation'
                    stopping = True
                    if self.startup is not None:
                        self.control('stop', reason=reason)
                    elif foreground_rc is None:
                        self.foreground.terminate()
                if (not stopping and records.ready and not acknowledged and self.startup is not None
                        and foreground_rc is None and monitor_rc is None):
                    # Manager/kernel observation above can block. Its earlier
                    # cached alive result cannot release a now-dead monitor.
                    foreground_rc, monitor_rc = self.foreground.status(), monitor.status()
                    if foreground_rc is not None or monitor_rc is not None:
                        failed = True
                        continue
                    # Owner output/cancellation/monitor handling is installed.
                    writer, ack_write = ack_write, None
                    self.modules['queued'].StartupGate(self.queue, self.proof, self.context,
                        self.reserved).send(writer, ack)
                    acknowledged = True
                if foreground_rc is not None:
                    if self.startup is not None and self.final_deadline is None:
                        self.final_deadline = self.clock() + 2 * self.startup['scope']['stop_usec'] / 1_000_000
                    if monitor_rc is None and not monitor_stopped:
                        monitor_stopped = True
                        os.close(monitor_write)
                        monitor_write = None
                    if self.startup is not None:
                        cleanup = self.modules['scope'].cleanup_observation(self.startup['scope'],
                            self.native.digest(self.startup['scope']), self.proof, owned_wait=True)
                        if 'facts' in cleanup:
                            self._retain_facts(cleanup['facts'])
                        if cleanup['state'] == 'unresolved' and not stopping:
                            stopping = True
                            self.control('stop', reason='cancellation')
                    if ended and monitor_rc is not None and (cleanup is None or cleanup['state'] != 'unresolved'):
                        try:
                            os.killpg(monitor.identity['pid'], 0)
                        except ProcessLookupError:
                            break  # Actual group absence plus EOF, not leader exit alone.
                deadline = self.final_deadline if self.final_deadline is not None else self.work_deadline
                remaining = deadline - self.clock()
                if remaining <= 0:
                    if self.final_deadline is None and not stopping:
                        # Work can expire after this iteration's stop check.
                        # Re-enter that transition; EOF alone cannot stop work.
                        continue
                    return None  # Unaccounted producer/child/scope: leave NULL/open.
                if not ended and select.select([output_read], [], [], min(.1, remaining))[0]:
                    raw = os.read(output_read, self.proof._LIMIT)
                    try:
                        if not raw:
                            ended = True
                            records.eof()
                        else:
                            for event in records.feed(raw):
                                if monitor_stopped:
                                    continue  # Samples were retained; late actions cannot act.
                                if event in ('freeze', 'thaw'):
                                    self.control(event)
                                elif event in ('wall', 'pressure_stop', 'owned_oom'):
                                    stopping = True
                                    self.control('stop', reason='pressure_stop' if event == 'owned_oom' else event)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        failed = True
                elif ended:
                    time.sleep(min(.1, max(0, deadline - self.clock())))
            # No blocking queue publication until physical/control producers end.
            if self.first_facts is not None and self.startup is None:
                return None  # No complete manager header: never replace or invent it.
            self.queue.cancel_launch(self.context)
            state = self.queue.execution_status(self.context)
            if state['scope_registration'] is not None and cleanup is not None:
                self.queue.remember_native_event(self.context, 'cleanup', cleanup)
            # Successful waits cannot certify normal control after a retained
            # local kill that H does not represent as resource. Keep the raw
            # sample and existing uncertainty; never invent a late stop.
            local_uncertainty = (self.startup is not None
                and self.modules['queued'].sampled_owned_oom({'facts': self.startup['facts']}) > 0
                and self.native.owned_oom({'startup': self.startup}) == 0)
            reason = ('startup_incomplete' if state['native_intent'] is None
                      and (state['scope_registration'] is None or local_uncertainty)
                      else 'monitor_failed' if failed or monitor_rc != 0 or local_uncertainty else 'normal')
            result = dict(state='clean' if reason == 'normal' else 'uncertain', reason=reason,
                reservation_sha256=self.native.digest(self.reserved), observer=self.reserved['owner'],
                observed_ns=time.monotonic_ns(), foreground=dict(identity=self.foreground.identity, returncode=foreground_rc),
                monitor=dict(identity=monitor.identity, returncode=monitor_rc), stop=self.stop)
            if self.startup is not None:
                result['startup'] = self.startup
            self.queue.remember_native_event(self.context, 'control-closure', result)
            return result
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
            self.diagnostics.append(type(error).__name__)
            if self.foreground is not None:
                try:
                    if self.startup is not None:
                        self.control('stop', reason='cancellation')
                    else:
                        self.foreground.terminate()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as stop_error:
                    self.diagnostics.append(type(stop_error).__name__)
            return None
        finally:
            for fd in (monitor_read, monitor_write):
                if fd is not None:
                    os.close(fd)  # Retire on every failure/return; EOF is not completion proof.
            if monitor is not None:
                try:
                    monitor.status()
                except (OSError, ValueError, RuntimeError) as error:
                    self.diagnostics.append(type(error).__name__)
            for fd in (ack_read, ack_write, output_read, output_write):
                if fd is not None:
                    os.close(fd)
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def early(self):
        if self.startup is not None or self.foreground is None or self.foreground.status() is not None:
            raise ValueError('startup requires one original unreaped foreground sample')
        unit = self.reserved['unit']
        path = self.manager._object('GetUnit', 's', unit + '.scope')
        facts = self.probe.capture(pid=self.foreground.identity['pid'])
        if facts['reporter'] != self.foreground.identity:
            raise ValueError('startup kernel sample names another foreground')
        self._retain_facts(facts)
        if self.modules['queued'].sampled_owned_oom({'facts': facts}) > 0:
            self.startup_error = True
        scope = self.manager.registration(unit, facts, wall_seconds=self.reserved['wall_seconds'])
        self.native.registration_data(scope, self.proof)
        self.native._bound_facts(scope['facts'], self.reserved, self.proof)
        if (scope['facts'] != facts or facts['reporter'] != self.foreground.identity
                or self.manager._object('GetUnit', 's', unit + '.scope') != path):
            raise ValueError('original foreground or manager changed during startup')
        self.object_path = path
        scope = {**scope, 'facts': self.first_facts}
        self.startup = json.loads(self.native.encode(dict(scope=scope, facts=facts,
                                                          observed_ns=self.owner_observed_ns)))
        if max(self.modules['queued'].sampled_owned_oom(self.startup['scope']),
               self.modules['queued'].sampled_owned_oom({'facts': self.startup['facts']})) > 0:
            raise ValueError('startup scope already has an owned OOM')
        value = dict(version=1, reservation_sha256=self.native.digest(self.reserved),
                     owner=self.reserved['owner'], foreground=self.foreground.identity,
                     unit_path=path, scope=scope)
        return self.modules['queued'].StartupGate(
            self.queue, self.proof, self.context, self.reserved).validate(value)

    def _current(self):
        scope = self.startup['scope']
        try:
            alive = self.foreground.status() is None
        except ChildProcessError:
            alive = False
        if alive:
            facts = self.probe.capture(pid=self.foreground.identity['pid'])
            self._retain_facts(facts)
            current = self.manager.registration(scope['unit'], facts, wall_seconds=self.reserved['wall_seconds'])
            self.native.registration_data(current, self.proof)
            if (current['facts'] != facts or any(current[key] != scope[key]
                    for key in ('unit', 'invocation_id', 'stop_usec'))
                    or self.manager._object('GetUnit', 's', scope['unit'] + '.scope') != self.object_path):
                raise ValueError('original scope manager identity changed')
        else:
            observation = self.modules['scope'].cleanup_observation(
                scope, self.native.digest(scope), self.proof,
                owned_wait=self.foreground._result is not None)
            if observation['state'] in ('removed', 'rebooted'):
                return None
            facts = observation['facts']
            self._retain_facts(facts)
            if observation['state'] == 'quiescent':
                return None
            self.manager.original(scope, facts, object_path=self.object_path,
                                  wall_seconds=self.reserved['wall_seconds'], proof=self.proof)
        return facts

    def _retain_facts(self, facts):
        """Advance this owner's sequence before a later manager read can fail."""
        self.native._bound_facts(facts, self.reserved, self.proof)
        if self.owner_facts is not None:
            try:
                self.proof._complete(self.owner_facts, facts)
            except ValueError:
                self.startup_error = True
                raise
        self.owner_facts = json.loads(self.native.encode(facts))
        self.owner_observed_ns = time.monotonic_ns()
        if self.first_facts is None:
            self.first_facts = json.loads(self.native.encode(facts))
        if self.startup is not None:
            self._select_facts(facts, self.owner_observed_ns)

    def _select_facts(self, facts, observed_ns):
        """Keep one ACTUAL strongest owned-OOM sample, never delivery-order facts."""
        # Hierarchy preserves H resource evidence; local kills then outrank
        # zero regardless of delivery time. Never merge two actual snapshots.
        rank = (facts['events']['oom_kill'], facts['local_events']['oom_kill'], observed_ns)
        prior = self.startup['facts']
        previous = (prior['events']['oom_kill'], prior['local_events']['oom_kill'], self.startup['observed_ns'])
        if rank > previous:
            self.startup.update(facts=json.loads(self.native.encode(facts)), observed_ns=observed_ns)

    def _systemctl(self, action, unit, remaining):
        subprocess.run(['/usr/bin/systemctl', '--user', action, unit + '.scope'],
                       check=True, timeout=remaining, close_fds=True,
                       env=self.modules['unit'].systemctl_env())

    def control(self, action, *, reason=None):
        if self.startup is None or action not in ('freeze', 'thaw', 'stop'):
            raise ValueError('control requires its original observed scope and a supported action')
        if action == 'stop':
            if reason not in ('pressure_stop', 'wall', 'cancellation'):
                raise ValueError('stop requires its actual supported reason')
            if self.final_deadline is None:
                self.final_deadline = self.clock() + 2 * self.startup['scope']['stop_usec'] / 1_000_000
            deadline = self.final_deadline
        else:
            if reason is not None or self.final_deadline is not None:
                raise ValueError('pause/thaw cannot renew terminal control')
            deadline = self.work_deadline
        if deadline <= self.clock():
            raise TimeoutError('original final control deadline reached')
        self.manager.deadline = deadline
        if self._current() is None:
            return False  # Physical end is not a new resource stop.
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError('original final control deadline reached')
        stop = None if action != 'stop' else dict(reason=reason, observed_ns=time.monotonic_ns(),
            boot_id=self.reserved['owner']['boot_id'], unit=self.reserved['unit'])
        self._control(action, self.reserved['unit'], remaining)
        if stop is not None:
            self.stop = stop
            try:
                self.queue.remember_native_event(self.context, 'stop', stop)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                # Physical control has already been accepted. Keep the entire
                # observation for the final closure even if publication failed.
                self.diagnostics.append(type(error).__name__)
        return True
