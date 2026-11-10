"""Queued native seam shared by the accepted CBM adapter and pinned GN reporter.

This module never reserves or launches a systemd scope. The existing outer
owner retains locks, watchdog, descendant cleanup and the actual scope wait.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import runpy
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

# This child changes only its own selection score, then preserves its PID across
# exec. No mutable project source is reopened after durable native intent.
_NATIVE_EXEC = """import os,sys
if os.environ.get('CODE_INTEL_TEST_FORCE_OOM_ADJ_FAILURE') == '1':
    raise SystemExit(125)
with open('/proc/self/oom_score_adj', 'w') as stream:
    stream.write('1000\\n')
with open('/proc/self/oom_score_adj') as stream:
    if stream.read().strip() != '1000':
        raise SystemExit(125)
os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
"""


@dataclass(frozen=True, repr=False)
class NativeEnvironment:
    """Private native values plus one manager-bound registration substitution."""

    frozen: Mapping[str, str]
    _native: object

    def __init__(self, value, native):
        native._object(value, ("frozen", "registration_slots"), "native environment recipe")
        slots = native._object(value["registration_slots"], ("INVOCATION_ID",),
                               "native environment registration slots")
        frozen = value["frozen"]
        if (slots["INVOCATION_ID"] != "scope.invocation_id" or not isinstance(frozen, dict)
                or "INVOCATION_ID" in frozen
                or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                       or not isinstance(entry, str) or "\0" in entry for key, entry in frozen.items())):
            raise ValueError("invalid frozen native environment recipe")
        object.__setattr__(self, "frozen", MappingProxyType(dict(frozen)))
        object.__setattr__(self, "_native", native)

    @property
    def recipe(self):
        return dict(frozen=dict(self.frozen),
                    registration_slots={"INVOCATION_ID": "scope.invocation_id"})

    def resolve(self, invocation_id, actual_control_environment):
        self._native._hex(invocation_id, 32, "scope invocation")
        if (not isinstance(actual_control_environment, Mapping)
                or actual_control_environment.get("INVOCATION_ID") != invocation_id):
            raise ValueError("reporter invocation differs from registered scope")
        return MappingProxyType({**self.frozen, "INVOCATION_ID": invocation_id})


def sampled_owned_oom(scope):
    """Fresh validated samples may expose a later kill in either counter file."""
    facts = scope["facts"]
    return max(facts["events"]["oom_kill"], facts["local_events"]["oom_kill"])


class StartupGate:
    """One bounded startup ACK; the caller still owns live identity and control."""

    def __init__(self, queue, proof, context, reserved):
        self.native, self.proof = queue._native, proof
        checked = self.native.context(context, queue._legs, queue.marker_hash)
        self._reserved = self.native.encode(self.native.reservation(reserved, checked))

    def validate(self, value):
        native, reserved = self.native, json.loads(self._reserved)
        value = json.loads(native.encode(value), object_pairs_hook=self.proof._unique)
        native._object(value, ("version", "reservation_sha256", "owner", "foreground", "unit_path", "scope"), "startup ACK")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("invalid startup ACK version")
        native._hex(value["reservation_sha256"], 64, "startup reservation digest")
        for name in ("owner", "foreground"):
            identity = native._object(value[name], ("pid", "start_ticks", "boot_id"), "startup process identity")
            native._integer(identity["pid"], "startup pid", positive=True)
            native._integer(identity["start_ticks"], "startup start", positive=True)
            native._boot(identity["boot_id"])
        scope = native.registration_data(value["scope"], self.proof)
        native._bound_facts(scope["facts"], reserved, self.proof)
        if (value["reservation_sha256"] != native.digest(reserved) or value["owner"] != reserved["owner"]
                or value["foreground"] != scope["facts"]["reporter"]
                or value["foreground"]["pid"] == value["owner"]["pid"]
                or scope["unit"] != reserved["unit"]
                or 2 * scope["stop_usec"] > reserved["wall_seconds"] * 1_000_000
                or not isinstance(value["unit_path"], str)
                or re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", value["unit_path"]) is None):
            raise ValueError("startup ACK differs from original reservation or child")
        if sampled_owned_oom(scope) > 0:
            raise ValueError("startup scope already has an owned OOM")
        return value

    def verify(self, value, probe, manager, actual_environment, *, proc=Path('/proc')):
        """Bind fresh facts to this reporter and its still-live original parent.

        Positive fresh OOM facts are returned for the caller to relay before
        refusing preparation; verification alone never grants native execution.
        """
        value = self.validate(value)
        if not isinstance(actual_environment, Mapping):
            raise ValueError('startup requires actual control environment')
        reserved = json.loads(self._reserved)
        pid, parent = os.getpid(), os.getppid()
        before = self.proof._process_state(proc / 'self', pid)
        owner = self.proof._process_state(proc / str(parent), parent)
        boot = (proc / 'sys/kernel/random/boot_id').read_text().strip()
        if (before[2] != parent or before[:2] != (value['foreground']['pid'], value['foreground']['start_ticks'])
                or owner[:2] != (value['owner']['pid'], value['owner']['start_ticks'])
                or boot != value['owner']['boot_id'] or boot != value['foreground']['boot_id']):
            raise ValueError('startup reporter or original parent identity differs')
        unit = reserved['unit']
        if manager._object('GetUnit', 's', unit + '.scope') != value['unit_path']:
            raise ValueError('startup original manager object differs')
        facts = probe.capture()
        current = manager.registration(unit, facts, wall_seconds=reserved['wall_seconds'])
        self.native.registration_data(current, self.proof)
        self.native._bound_facts(current['facts'], reserved, self.proof)
        if (current['facts'] != facts or facts['reporter'] != value['foreground']
                or any(current[key] != value['scope'][key] for key in ('unit', 'invocation_id', 'stop_usec'))
                or manager._object('GetUnit', 's', unit + '.scope') != value['unit_path']
                or actual_environment.get('INVOCATION_ID') != current['invocation_id']):
            raise ValueError('startup original scope or actual invocation differs')
        self.proof._complete(value['scope']['facts'], facts)
        if (os.getppid() != parent or self.proof._process_state(proc / 'self', pid) != before
                or self.proof._process_state(proc / str(parent), parent) != owner
                or (proc / 'sys/kernel/random/boot_id').read_text().strip() != boot):
            raise ValueError('startup reporter or original parent changed during verification')
        return current

    def observation(self, scope, value):
        reserved = json.loads(self._reserved)
        self.native.registration_data(scope, self.proof)
        self.native._bound_facts(scope['facts'], reserved, self.proof)
        self.native._object(value, ('reservation_sha256', 'scope_sha256', 'facts', 'observed_ns'),
                            'startup output observation')
        self.native._integer(value['observed_ns'], 'startup observation time', positive=True)
        if (value['reservation_sha256'] != self.native.digest(reserved)
                or value['scope_sha256'] != self.native.digest(scope)
                or value['observed_ns'] > time.monotonic_ns()):
            raise ValueError('startup output differs from original reservation, scope or clock')
        self.native._bound_facts(value['facts'], reserved, self.proof)
        self.proof._complete(scope['facts'], value['facts'])
        return json.loads(self.native.encode(value))

    def emit(self, fd, scope, value):
        """One atomic sample; its caller keeps the existing output until exit."""
        self._pipe(fd, os.O_WRONLY)
        limit = self._limit(fd)
        raw = self.native.encode(self.observation(scope, value)) + b'\n'
        if len(raw) > limit:
            raise ValueError('startup output exceeds atomic transport capacity')
        os.set_blocking(fd, False)
        if os.write(fd, raw) != len(raw):
            raise OSError(errno.EIO, 'incomplete startup output write')

    def _pipe(self, fd, mode):
        if type(fd) is not int or fd < 3:
            raise ValueError("invalid startup descriptor")
        if (not stat.S_ISFIFO(os.fstat(fd).st_mode)
                or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != mode
                or re.fullmatch(r"pipe:\[[0-9]+\]", os.readlink(f"/proc/self/fd/{fd}")) is None):
            raise ValueError("startup requires its anonymous pipe and exact access mode")
    def _limit(self, fd):
        os.set_inheritable(fd, False)
        limit = min(self.proof._LIMIT, os.fpathconf(fd, "PC_PIPE_BUF"))
        if limit <= 0:
            raise ValueError("startup pipe atomic capacity unavailable")
        return limit

    def send(self, fd, value):
        self._pipe(fd, os.O_WRONLY)
        try:
            limit = self._limit(fd)
            raw = self.native.encode(self.validate(value)) + b"\n"
            if len(raw) > limit:
                raise ValueError("startup ACK exceeds its atomic transport capacity")
            os.set_blocking(fd, False)
            if os.write(fd, raw) != len(raw):
                raise OSError(errno.EIO, "incomplete startup ACK write")
        finally:
            os.close(fd)

    def receive(self, fd):
        self._pipe(fd, os.O_RDONLY)
        try:
            limit = self._limit(fd)
            raw = b""
            while len(raw) <= limit:
                part = os.read(fd, limit + 1 - len(raw))
                if not part:
                    break
                raw += part
            if (len(raw) > limit or not raw.startswith(b"{") or not raw.endswith(b"}\n")
                    or raw.count(b"\n") != 1):
                raise ValueError("incomplete, ambiguous or oversized startup ACK")
            return self.validate(json.loads(raw, object_pairs_hook=self.proof._unique))
        finally:
            os.close(fd)


def cbm_source(*, snapshot, worker, config, args, cap, settings, environment, policy,
               registration_slots=None):
    """Hash effective startup inputs; retain no settings or environment values.

    The accepted ABI fixes argv construction; the disposable response filename
    is owned by that adapter under the bound cache directory. Its random suffix
    is not execution authority. No caller-provided command is substituted.
    """
    value = dict(
        settings_sha256=hashlib.sha256(settings).hexdigest(), config=dict(config),
        repo_path=args.repo_path, mode=args.mode, persistence=args.persistence,
        cap_bytes=cap, native_budget_bytes=cap * 3 // 4,
        environment=dict(environment), policy=dict(policy),
    )
    # The captured worker source binds argument construction in snapshot.files.
    if registration_slots is not None:
        value["registration_slots"] = dict(registration_slots)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return dict(commit=snapshot.commit, files=dict(snapshot.files), provider_digest=worker.BUILD,
                provider_version="v0.11.0-stock-worker",
                configuration_sha256=hashlib.sha256(encoded).hexdigest())


def effective_policy(value, native, *, full=False):
    """One copied policy identity for owner control and reporter/native input."""
    fields = {"route", "slice", "wall_seconds", "sibling_reserve_bytes", "cache_reserve_bytes"}
    complete = fields | {"io_weight", "cpu_quota", "watchdog"}
    if not isinstance(value, dict) or set(value) not in ((complete,) if full else (fields, complete)):
        raise ValueError("invalid effective worker policy fields")
    for key in ("wall_seconds", "sibling_reserve_bytes", "cache_reserve_bytes"):
        native._integer(value[key], key, positive=key == "wall_seconds")
    if (value["route"] not in ("workload", "capped", "inherit")
            or not isinstance(value["slice"], str)
            or re.fullmatch(r"[A-Za-z0-9_.\\-]+\.slice", value["slice"]) is None):
        raise ValueError("invalid effective worker route")
    if set(value) == complete:
        def number(raw, *, positive=True, integer=False):
            pattern = r"[0-9]+" if integer else r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
            if not isinstance(raw, str) or re.fullmatch(pattern, raw) is None:
                raise ValueError("invalid effective policy number")
            parsed = Decimal(raw)
            if not (parsed > 0 if positive else parsed >= 0) or parsed > native.INTEGER_MAX:
                raise ValueError("effective policy number outside supported range")
            return parsed

        if number(value["io_weight"], integer=True) > 10000:
            raise ValueError("invalid effective IO weight")
        quota = value["cpu_quota"]
        if not isinstance(quota, str) or not quota.endswith("%"):
            raise ValueError("invalid effective CPU quota")
        number(quota[:-1])
        watchdog = native._object(value["watchdog"], (
            "interval", "warmup_interval", "warmup_seconds", "load_max", "iowait_max",
            "bad_samples", "continuous_pause_max"), "effective watchdog policy")
        for key, raw in watchdog.items():
            number(raw, positive=key not in ("warmup_seconds", "load_max", "iowait_max"),
                   integer=key in ("warmup_seconds", "bad_samples", "continuous_pause_max"))
    return json.loads(native.encode(value))


def runtime_inputs(snapshot, modules):
    """Decode complete private provider inputs without queue or process I/O."""
    value = snapshot.runtime_value
    if snapshot.profile != 'runner' or value is None:
        raise ValueError('executable runtime role requires its private runner capsule')
    queue, native = modules['queue'], modules['queue']._native
    native._object(value, ('context', 'reservation', 'policy', 'provider_options', 'environment_recipe'),
                   'private runtime')
    context = native.context(value['context'], queue._legs, queue.marker_hash)
    reserved = native.reservation(value['reservation'], context)
    policy = effective_policy(value['policy'], native, full=True)
    if (reserved['source']['commit'] != snapshot.commit or reserved['source']['files'] != dict(snapshot.files)
            or any(policy[key] != reserved[key] for key in ('route', 'slice', 'wall_seconds'))):
        raise ValueError('runtime source or policy differs from its reservation')
    if context['provider'] == 'gitnexus':
        modules['gitnexus'].validate(snapshot, modules, value)
    else:
        options = native._object(value['provider_options'], ('managed_config', 'persistence'), 'CBM runtime options')
        modules['unit'].absolute(options['managed_config'])
        if (options['persistence'] not in ('true', 'false')
                or reserved['source']['provider_digest'] != modules['worker'].BUILD
                or reserved['source']['provider_version'] != 'v0.11.0-stock-worker'):
            raise ValueError('runtime provider inputs differ from the accepted worker')
    return value, NativeEnvironment(value['environment_recipe'], native)


def reporter(snapshot, modules, ack_fd):
    """The fixed scoped reporter; ACK and live ownership precede preparation."""
    output = None
    try:
        value, environment = runtime_inputs(snapshot, modules)
        queue, proof = modules['queue'], modules['proof']
        context, reserved = value['context'], value['reservation']
        gate = StartupGate(queue, proof, context, reserved)
        gate._pipe(ack_fd, os.O_RDONLY)
        identity = os.fstat(ack_fd)
        if any((identity.st_dev, identity.st_ino) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
               for fd in (0, 1, 2)):
            raise ValueError('startup descriptor aliases a standard stream')
        control_output, log_output = os.fstat(1), os.fstat(2)
        if (control_output.st_dev, control_output.st_ino) == (log_output.st_dev, log_output.st_ino):
            raise ValueError('private control output aliases provider logging')
        ack = gate.receive(ack_fd)
        output = os.dup(1)
        os.dup2(2, 1)  # Provider stdout is a log, never another control producer.
        gate._pipe(output, os.O_WRONLY)
        gate._limit(output)
        probe = proof.ScopeProbe(reserved['unit'], reserved['cap_bytes'],
                                 route=(reserved['route'], reserved['slice']))
        manager = modules['scope'].ScopeManager(modules['unit'].bus_call, modules['unit'].typed_property,
                                                deadline=time.monotonic() + reserved['wall_seconds'])

        def observe(observation):
            gate.emit(output, ack['scope'], observation)

        def live():
            current = gate.verify(ack, probe, manager, dict(os.environ))
            observe(dict(reservation_sha256=queue._native.digest(reserved),
                         scope_sha256=queue._native.digest(ack['scope']), facts=current['facts'],
                         observed_ns=time.monotonic_ns()))
            if sampled_owned_oom(current) > 0:
                raise ValueError('startup scope has an owned OOM')

        live()
        if context['provider'] == 'gitnexus':
            return modules['gitnexus'].run(snapshot, modules, value, environment,
                                          startup=ack['scope'], observe=observe, verify=live)
        factory = cbm_factory(snapshot, modules, context, value['policy'], environment_recipe=environment)

        def bound_factory(*args):
            run = factory(*args)
            run.bind_startup(ack['scope'], observe, verify=live)
            return run

        options = value['provider_options']
        return modules['worker'].main([
            '--repo-path', context['repo_path'], '--mode', reserved['run_mode'],
            '--persistence', options['persistence'], '--managed-config', options['managed_config']],
            queued_factory=bound_factory, queued_native_environment=environment)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('code-intel: captured reporter refused/failed: ' + type(error).__name__, file=sys.stderr)
        return 125
    finally:
        if output is not None:
            os.close(output)


def cbm_factory(snapshot, modules, context, policy, *, environment_recipe=None):
    """Use the actual captured runtime inputs at registration and native intent."""
    queue = modules["queue"]
    context = queue._native.context(context, queue._legs, queue.marker_hash)
    if context["provider"] != "cbm":
        raise ValueError("CBM factory requires its provider claim")
    policy = effective_policy(policy, queue._native)
    if environment_recipe is not None and not isinstance(environment_recipe, NativeEnvironment):
        raise ValueError("queued factory requires its validated native environment recipe")

    def factory(config, args, cap, settings, environment):
        if environment_recipe is not None and dict(environment) != dict(environment_recipe.frozen):
            raise ValueError("actual native environment differs from its frozen recipe")
        reserved = queue.execution_status(context)["launch_reservation"]
        actual = dict(run_mode=args.mode, cap_bytes=cap,
                      **{key: policy[key] for key in ("route", "slice", "wall_seconds")})
        if (reserved is None or any(reserved[key] != value for key, value in actual.items())
                or args.persistence not in ("true", "false")):
            raise ValueError("actual worker policy differs from its reservation")

        def source():
            if not snapshot.current_matches():
                raise ValueError("captured source is no longer current before native intent")
            return cbm_source(snapshot=snapshot, worker=modules["worker"], config=config,
                              args=args, cap=cap, settings=settings, environment=environment,
                              policy=policy, registration_slots=(None if environment_recipe is None else
                                                                environment_recipe.recipe["registration_slots"]))

        def admit():
            modules["admission"].assess(
                cap, policy["sibling_reserve_bytes"], reserved["unit"], provider="cbm",
                cache_reserve=policy["cache_reserve_bytes"])

        probe = modules["proof"].ScopeProbe(reserved["unit"], cap,
                                              route=(policy["route"], policy["slice"]))
        manager = modules["scope"].ScopeManager(
            modules["unit"].bus_call, modules["unit"].typed_property,
            deadline=time.monotonic() + policy["wall_seconds"])
        return QueuedRun(queue, modules["proof"], probe, manager, context,
                         source=source, admit=admit)

    return factory


def executable_identity(stream):
    """Hash the accepted open inode without disturbing the native descriptor."""
    fd = stream.fileno()
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("accepted executable must be regular")
    digest, offset = hashlib.sha256(), 0
    while chunk := os.pread(fd, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if any(getattr(before, field) != getattr(after, field)
           for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
        raise ValueError("accepted executable changed while reading")
    return dict(device=before.st_dev, inode=before.st_ino, sha256=digest.hexdigest())


class QueuedRun:
    """One reporter uses one retained reservation and the actual observer wait."""

    def __init__(self, queue, proof, probe, manager, context, *, source, admit, cleanup=None):
        self.queue, self.proof, self.probe, self.manager = queue, proof, probe, manager
        self.context = queue._native.context(context, queue._legs, queue.marker_hash)
        status = queue.execution_status(self.context)
        self.reserved = status["launch_reservation"]
        if self.reserved is None or status["launch_permission"] != "reserved":
            raise RuntimeError("reporter requires one open durable reservation")
        queue._native.reservation(self.reserved, self.context)
        if status["scope_registration"] is not None or status["native_intent"] is not None:
            raise RuntimeError("another reporter already owns this reservation")
        self.source, self.admit = source, admit
        # Capture before registration/native intent. The source-bound assembly
        # supplies its preloaded callable; the explicit prototype default also
        # loads once here, never from a mutable path after known completion.
        self.cleanup = cleanup if cleanup is not None else globals().get("_CAPTURED_CLEANUP")
        if self.cleanup is None:
            self.cleanup = runpy.run_path(str(Path(__file__).with_name("code_intel_scope.py")))["cleanup_observation"]
        if not callable(self.cleanup):
            raise ValueError("queued reporter requires a captured cleanup callable")
        self.registration = self.report = self.launch = None
        self._startup_scope = self._startup_observer = self._startup_verify = None

    def bind_startup(self, scope, observe, *, verify=None):
        """Retain the original owner sample before fresh reporter registration."""
        if self._startup_scope is not None or self.registration is not None:
            raise RuntimeError("startup binding is single-use before registration")
        native = self.queue._native
        scope = json.loads(native.encode(native.registration_data(scope, self.proof)))
        native._bound_facts(scope["facts"], self.reserved, self.proof)
        if (scope["unit"] != self.reserved["unit"]
                or 2 * scope["stop_usec"] > self.reserved["wall_seconds"] * 1_000_000
                or not callable(observe) or verify is not None and not callable(verify)):
            raise ValueError("startup binding differs from reserved scope or observer")
        self._startup_scope, self._startup_observer = scope, observe
        self._startup_verify = verify

    def _source(self):
        current = self.queue._native.source_identity(self.source())
        if current != self.reserved["source"]:
            raise ValueError("loaded source, provider or configuration changed")
        return current

    def register(self):
        """Register before the adapter's bounded native readiness queries."""
        if self.registration is not None:
            raise RuntimeError("registration is single-use")
        self._source()
        if self._startup_verify is not None:
            self._startup_verify()
        facts = self.probe.capture()
        registered = self.manager.registration(self.reserved["unit"], facts,
                                               wall_seconds=self.reserved["wall_seconds"])
        native = self.queue._native
        native.registration_data(registered, self.proof)
        native._bound_facts(registered["facts"], self.reserved, self.proof)
        if self._startup_scope is not None:
            early = self._startup_scope
            if any(early[key] != registered[key] for key in ("unit", "invocation_id", "stop_usec")):
                raise ValueError("startup scope lifetime changed before registration")
            self.proof._complete(early["facts"], registered["facts"])
            observation = dict(reservation_sha256=native.digest(self.reserved),
                               scope_sha256=native.digest(early), facts=registered["facts"],
                               observed_ns=time.monotonic_ns())
            self._startup_observer(json.loads(native.encode(observation)))
            if sampled_owned_oom(early) > 0 or sampled_owned_oom(registered) > 0:
                raise ValueError("startup scope has an owned OOM")
            registered = json.loads(native.encode(early))
        elif sampled_owned_oom(registered) > 0:
            raise ValueError("startup scope has an owned OOM")
        if self.queue.register_scope(self.context, registered) != "registered":
            raise RuntimeError("first scope registration not acknowledged")
        self.registration = registered
        return registered

    def native(self, command, executable, *, after_spawn, pass_fds=(), env=None, cwd=None):
        if self.registration is None:
            raise RuntimeError("native seam requires registration")

        def begin(facts):
            try:
                source = self._source()
                if self._startup_verify is not None:
                    self._startup_verify()
                current = self.manager.registration(self.reserved["unit"], facts,
                                                    wall_seconds=self.reserved["wall_seconds"])
                self.queue._native.registration_data(current, self.proof)
                self.queue._native._bound_facts(current["facts"], self.reserved, self.proof)
                if (current["facts"] != facts or any(current[key] != self.registration[key]
                        for key in ("unit", "invocation_id", "stop_usec"))):
                    raise ValueError("original scope manager identity changed before native intent")
                self.proof._complete(self.registration["facts"], current["facts"])
                accepted = executable_identity(executable)
                if accepted["sha256"] != source["provider_digest"]:
                    raise ValueError("native inode differs from accepted provider")
                self.admit()
                return self.queue.begin_native(self.context, dict(
                    source=source, executable=accepted, facts=facts,
                    registration_sha256=self.queue._native.digest(self.registration),
                    before_ns=time.monotonic_ns()))
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                # The observer still owns Popen. A refused/unknown acknowledgement
                # cannot create a child, including after a successful DB commit.
                raise ValueError("native intent refused or unacknowledged") from error

        self.launch = self.proof.QueuedLaunch(
            baseline=self.registration["facts"], before_spawn=begin, after_spawn=after_spawn,
            pass_fds=pass_fds, env=env, cwd=cwd)
        self.report = self.proof.observe(self.probe,
                                         [sys.executable, "-I", "-S", "-B", "-c", _NATIVE_EXEC, *command],
                                         seconds=self.reserved["wall_seconds"],
                                         launch=self.launch)
        return self.report

    def refuse_preparation(self):
        """Retain fresh registered facts without claiming a native attempt."""
        if self.registration is None or self.report is not None or self.launch is not None:
            raise RuntimeError("preparation refusal requires an unlaunched registration")
        facts = self.probe.capture()
        self.proof._complete(self.registration["facts"], facts)
        self.report = dict(version=1, outcome="refused", payload_started=False,
                           payload_returncode=None, before=facts, after=None,
                           finality=None, error="proof_unavailable")
        self.terminal(provider_ok=False)

    def terminal(self, *, provider_ok):
        if self.report is None or type(provider_ok) is not bool:
            raise ValueError("validated native report and explicit provider result required")
        self.proof.validate_report(self.report)
        if not self.report["payload_started"]:
            self.queue.cancel_launch(self.context)  # A spent intent stays spent.
        self.queue.remember_native_event(self.context, "terminal", dict(
            report=self.report, provider_ok=provider_ok, source=self.reserved["source"],
            sealed_ns=time.monotonic_ns()))
        if self.launch is not None and self.launch.diagnostics:
            # Preserve known completion first. Administrative failure separately
            # retains uncertainty through an actual still-live cleanup sample.
            data = self.cleanup(
                self.registration, self.queue._native.digest(self.registration), self.proof,
                owned_wait=type(self.report["payload_returncode"]) is int, proc=self.probe.proc)
            self.queue.remember_native_event(self.context, "cleanup", data)
            for message in self.launch.diagnostics:
                print("code-intel: " + message, file=sys.stderr)
