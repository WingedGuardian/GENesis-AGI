"""Queued native seam shared by the accepted CBM adapter and pinned GN reporter.

This module never reserves or launches a systemd scope. The existing outer
owner retains locks, watchdog, descendant cleanup and the actual scope wait.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sqlite3
import stat
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
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


def cbm_factory(snapshot, modules, context, policy, *, environment_recipe=None):
    """Use the actual captured runtime inputs at registration and native intent."""
    queue = modules["queue"]
    context = queue._native.context(context, queue._legs, queue.marker_hash)
    if context["provider"] != "cbm":
        raise ValueError("CBM factory requires its provider claim")
    if set(policy) != {"route", "slice", "wall_seconds", "sibling_reserve_bytes", "cache_reserve_bytes"}:
        raise ValueError("invalid effective worker policy fields")
    policy = dict(policy)
    for key in ("wall_seconds", "sibling_reserve_bytes", "cache_reserve_bytes"):
        queue._native._integer(policy[key], key, positive=key == "wall_seconds")
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
        facts = self.probe.capture()
        registered = self.manager.registration(self.reserved["unit"], facts,
                                               wall_seconds=self.reserved["wall_seconds"])
        native = self.queue._native
        native.registration_data(registered, self.proof)
        native._bound_facts(registered["facts"], self.reserved, self.proof)
        # These files are sampled separately: a later local read can expose a
        # kill that was not yet present in the earlier hierarchical read.
        if max(native.owned_oom(registered), registered["facts"]["local_events"]["oom_kill"]) > 0:
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
