"""Initial captured-owner policy; no provider discovery or workload subprocess."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


class RepositoryMissing(FileNotFoundError):
    """The checkout for a retained execution no longer exists."""


class RepositoryBusy(BlockingIOError):
    """The canonical repository lock is held by another writer."""


@contextmanager
def repository_lock(modules, context):
    """Take the existing freeze namespace on fd9; never replace or fall back."""
    queue = modules['queue']
    context = queue._native.context(context, queue._legs, queue.marker_hash)
    with repository_lock_for(modules, context['repo_path'], context['hash']) as descriptor:
        yield descriptor


@contextmanager
def repository_lock_for(modules, repo_path, h):
    """The same namespace before a trial exists; no fabricated context."""
    queue = modules['queue']
    try:
        repo = Path(repo_path).resolve(strict=True)
    except FileNotFoundError as error:
        raise RepositoryMissing("original repository is missing") from error
    if str(repo) != repo_path or h != queue.marker_hash(repo_path) or not (repo / '.git').is_dir():
        raise ValueError('typed owner requires a physical repository checkout')
    base = Path(os.environ.get('GENESIS_HOME') or str(Path.home() / '.genesis'))
    if not base.is_absolute():
        raise ValueError('typed owner requires the canonical absolute lock namespace')
    try:
        os.fstat(9)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
    else:
        raise ValueError('initial owner requires its own unoccupied repository descriptor')
    directory = base / 'locks'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ('code-intel-' + h + '.lock')
    raw, owned = None, False
    try:
        raw = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
        held, named = os.fstat(raw), path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(held.st_mode) or not stat.S_ISREG(named.st_mode)
                or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)):
            raise ValueError('canonical repository lock identity differs')
        try:
            fcntl.flock(raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RepositoryBusy("canonical repository lock is busy") from error
        if raw != 9:
            os.dup2(raw, 9, inheritable=False)
            os.close(raw)
        raw, owned = None, True
        yield 9
    finally:
        if raw is not None:
            os.close(raw)
        if owned:
            os.close(9)


def wall_seconds(modules, mode, environment):
    if mode not in ('fast', 'moderate', 'full'):
        raise ValueError('unsupported typed mode')
    suffix, default = ('FULL', '14400') if mode == 'full' else ('FAST', '3600')
    value = modules['admission'].number(environment.get('CODE_INTEL_WATCHDOG_WALL_' + suffix) or default, 'wall cap')
    return modules['native']._integer(value, 'wall cap', positive=True)


def policy(modules, provider, mode, manager, environment):
    """Bind existing complete controls before reserving an actual owner."""
    if provider not in ('cbm', 'gitnexus') or mode not in ('fast', 'moderate', 'full'):
        raise ValueError('unsupported typed provider or mode')
    def select(key, default):
        return environment.get(key) or default
    if select('CODE_INTEL_INDEX_DISABLE', '0') != '0':
        raise ValueError('indexing is disabled or its switch is invalid')
    workload = select('CODE_INTEL_WORKLOAD_SLICE', '0')
    if workload not in ('0', '1'):
        raise ValueError('invalid workload route')
    cap = select('CODE_INTEL_' + ('CBM' if provider == 'cbm' else 'GITNEXUS') + '_MEMORY_MAX',
                 select('CODE_INTEL_INDEX_MEMORY_MAX', '8G'))
    match = re.fullmatch(r'((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)([KkMmGg]?)', cap)
    if (match is None or not match[2] and '.' in match[1]
            or Decimal(match[1]) * {'': 1, 'K': 1024, 'M': 1024**2, 'G': 1024**3}[match[2].upper()] != 8 * 1024**3):
        raise ValueError('typed worker requires the full accepted 8 GiB cap')
    number = modules['admission'].number
    route = 'workload' if workload == '1' else 'inherit' if provider == 'cbm' else 'capped'
    slice_name = ('genesis-workload.slice' if route == 'workload' else 'app-capped.slice'
                  if route == 'capped' else manager.owner_slice(os.getpid()))
    knobs = dict(interval=('INTERVAL', '15'), warmup_interval=('WARMUP_INTERVAL', '5'),
                 warmup_seconds=('WARMUP_S', '30'), load_max=('LOAD_MAX', '4'),
                 iowait_max=('IOWAIT_MAX', '25'), bad_samples=('BAD_SAMPLES', '2'),
                 continuous_pause_max=('CONT_PAUSE_MAX', '600'))
    value = dict(route=route, slice=slice_name,
        wall_seconds=wall_seconds(modules, mode, environment),
        sibling_reserve_bytes=number(select('CODE_INTEL_SIBLING_RESERVE_BYTES', '2147483648'), 'sibling reserve'),
        cache_reserve_bytes=number(select('CODE_INTEL_FILE_CACHE_RESERVE_BYTES', '2147483648'), 'cache reserve'),
        io_weight=select('CODE_INTEL_INDEX_IO_WEIGHT', '20'), cpu_quota=select('CODE_INTEL_INDEX_CPU_QUOTA', '200%'),
        watchdog={key: select('CODE_INTEL_WATCHDOG_' + suffix, default) for key, (suffix, default) in knobs.items()})
    return modules['queued'].effective_policy(value, modules['native'], full=True)


@contextmanager
def prepared_inputs(snapshot, modules, repo_path, provider, run_mode, settings_path, selected, *, unit, recovery=False,
                    with_observer=False, observe_policy=None):
    """Hold prospective inputs, not an execution identity or launch permission.

    The concrete unit can be derived from an issued nonce before an execution
    sequence exists. The consumer retains the CBM lifecycle lease through CAS.
    """
    if (type(with_observer) is not bool or (with_observer and not callable(observe_policy))
            or (not with_observer and observe_policy is not None)):
        raise ValueError('invalid lease-held input observer')
    native, managed = modules['queue']._native, modules['managed']
    selected = modules['queued'].effective_policy(selected, native, full=True)
    repo = Path(native._text(repo_path, 'prospective repository')).resolve(strict=True)
    if (str(repo) != repo_path or not (repo/'.git').is_dir() or provider not in ('cbm', 'gitnexus')
            or run_mode not in ('fast', 'moderate', 'full')
            or re.fullmatch(r'code-intel-'+modules['queue'].marker_hash(repo_path)[:12]
                            +'-'+provider+r'-[0-9]+', unit) is None):
        raise ValueError('invalid prospective repository/provider/unit')
    if not snapshot.current_matches():
        raise ValueError('initial runtime requires its current captured provider adapter')
    inputs = dict(unit=unit, run_mode=run_mode, cap_bytes=8 * 1024**3,
        **{key: selected[key] for key in ('route', 'slice', 'wall_seconds')})
    with managed.lifecycle_lock(shared=True) if provider == 'cbm' else nullcontext():
        active = True
        def observe():
            if not active:
                raise RuntimeError('input observation lease is closed')
            current_policy = modules['queued'].effective_policy(
                observe_policy() if with_observer else selected, native, full=True)
            if with_observer and not snapshot.current_matches():
                raise ValueError('captured acceptance source changed')
            current_inputs = dict(inputs, **{key: current_policy[key]
                                  for key in ('route', 'slice', 'wall_seconds')})
            control = modules['owner'].control_environment(current_inputs, current_policy, modules['unit'])
            if provider == 'gitnexus':
                options, recipe, source = modules['gitnexus'].freeze(snapshot, modules,
                    dict(repo_path=repo_path, provider=provider), current_inputs, current_policy, control, recovery=recovery)
            else:
                persistence = os.environ.get('CODE_INTEL_INDEX_PERSISTENCE') or 'true'
                if persistence not in ('true', 'false'):
                    raise ValueError('invalid persistence policy')
                documents = []
                path = modules['unit'].config_path(settings_path)
                config = managed.runtime_config(path, capture=documents.append)
                if with_observer:
                    managed.require_enabled(config)
                if (len(documents) != 1 or type(documents[0]) is not bytes
                        or Path(config['main']) != snapshot.root or config['main'] != repo_path):
                    raise ValueError('initial runtime requires its one immutable primary configuration')
                with managed.verified_binary(Path(config['binary']), config['build']):
                    pass  # Read/hash/access only; no provider process before reservation.
                environment = managed.native_env(config, environment=control)
                environment.pop('INVOCATION_ID', None)
                recipe = dict(frozen=environment, registration_slots={'INVOCATION_ID': 'scope.invocation_id'})
                accepted = modules['queued'].NativeEnvironment(recipe, native)
                args = SimpleNamespace(repo_path=repo_path, mode=run_mode, persistence=persistence)
                source = modules['queued'].cbm_source(snapshot=snapshot, worker=modules['worker'],
                    config=config, args=args, cap=current_inputs['cap_bytes'], settings=documents[0],
                    environment=accepted.frozen, policy=current_policy, registration_slots=recipe['registration_slots'])
                options = dict(managed_config=str(path), persistence=persistence)
            native.source_identity(source)

            value = dict(source=source, provider_options=options, environment_recipe=recipe)
            if with_observer:
                value['policy_sha256'] = native.digest(current_policy)
            return value
        try:
            value = observe()
            if with_observer:
                value.pop('policy_sha256')
                value['observe'] = observe
            yield value
        finally:
            active = False


def reserve_runtime(snapshot, modules, context, run_mode, settings_path, selected, *, actual_owner):
    """Only the first acknowledged actual-self reservation returns launch input.

    Caller owns the canonical runner/repository locks. All capsule validation
    precedes the CAS; a lost acknowledgement never returns a resumable input.
    """
    queue, native = modules['queue'], modules['queue']._native
    owner = json.loads(native.encode(actual_owner))
    if owner != queue._recovery.owner_identity():
        raise ValueError('initial runtime requires the original actual owner')
    context = native.context(context, queue._legs, queue.marker_hash)
    if queue.execution_status(context)['nonce_origin'] == 'selected-recovery':
        raise ValueError('selected runtime requires its first supervised acknowledgement')
    selected = modules['queued'].effective_policy(selected, native, full=True)
    reserved = dict(unit=native.expected_unit(context), run_mode=run_mode,
        owner=owner, cap_bytes=8 * 1024**3,
        **{key: selected[key] for key in ('route', 'slice', 'wall_seconds')})
    with prepared_inputs(snapshot, modules, context['repo_path'], context['provider'], run_mode,
                         settings_path, selected, unit=reserved['unit']) as inputs:
        reserved['source'] = inputs['source']
        native.reservation(reserved, context)
        runtime = dict(context=context, reservation=reserved, policy=selected,
            provider_options=inputs['provider_options'], environment_recipe=inputs['environment_recipe'])
        bound = snapshot.with_runtime(runtime)
        modules['queued'].runtime_inputs(bound, modules)
        if owner != queue._recovery.owner_identity():
            raise ValueError('actual owner changed before reservation')
        if queue.reserve_launch(context, reserved) != 'reserved':
            raise ValueError('reservation acknowledgement did not permit one launch')
        return bound


def execute_runtime(bound, modules, *, actual_owner):
    """Execute the already acknowledged reservation in this same actual owner."""
    runtime = bound.runtime_value
    context, reserved, selected = (runtime[key] for key in ("context", "reservation", "policy"))
    queue = modules['queue']
    if reserved['owner'] != actual_owner or actual_owner != queue._recovery.owner_identity():
        raise ValueError('runtime belongs to another actual owner')
    if context['provider'] == 'gitnexus':
        modules['gitnexus'].require_origin(runtime, modules)
    manager = modules['scope'].ScopeManager(modules['unit'].bus_call, modules['unit'].typed_property,
        deadline=time.monotonic() + selected['wall_seconds'], require_runtime_limit=True)
    probe = modules['proof'].ScopeProbe(reserved['unit'], reserved['cap_bytes'],
                                       route=(selected['route'], selected['slice']))
    owner = modules['owner'].PhysicalOwner(modules, context, reserved, probe, manager)
    if owner.run(bound) is None:
        return None  # Retained native completion can coexist with open control.
    queue.reconcile_native()
    state = queue.apply_native(context)
    return state if state in ('consumed', 'pending', 'failed') else None


def main(snapshot, modules, raw_context, run_mode, settings_path):
    """Private captured-runner endpoint; transport status never prices native work."""
    queue = modules['queue']
    try:
        context = queue._native.context(json.loads(raw_context, object_pairs_hook=modules['proof']._unique),
                                        queue._legs, queue.marker_hash)
        environment = dict(os.environ)
        with repository_lock(modules, context):
            manager = modules['scope'].ScopeManager(modules['unit'].bus_call, modules['unit'].typed_property,
                deadline=time.monotonic() + wall_seconds(modules, run_mode, environment))
            actual_owner = queue._recovery.owner_identity()
            selected = policy(modules, context['provider'], run_mode, manager, environment)
            if actual_owner != queue._recovery.owner_identity():
                raise ValueError('actual owner changed during policy lookup')
            manager.require_no_batch(modules['unit'].cgroup_empty)
            if actual_owner != queue._recovery.owner_identity():
                raise ValueError('actual owner changed during batch census')
            bound = reserve_runtime(snapshot, modules, context, run_mode, settings_path, selected,
                                    actual_owner=actual_owner)
            # Admission time never subtracts from the selected workload window.
            state = execute_runtime(bound, modules, actual_owner=actual_owner)
            if state is None:
                return 76
            queue._write_machine([('1', 'native-outcome', context['hash'], context['provider'], raw_context, state)])
            return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('code-intel: initial owner failed: ' + type(error).__name__, file=sys.stderr)
        return 76
