"""Initial owner policy is read without provider/version/readiness subprocesses."""
import json
import os
import runpy
import sys
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


def prepared(tmp_path, monkeypatch):
    _, _, snapshot = case(tmp_path, monkeypatch, 'runner')
    modules = snapshot.assemble()
    path = Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner_admission.py'
    assert path.is_file(), 'initial owner admission must precede physical launch'
    namespace = runpy.run_path(str(path))
    calls = []
    manager = SimpleNamespace(owner_slice=lambda pid: calls.append(pid) or 'app.slice')
    return namespace, modules, manager, calls


@pytest.mark.parametrize('provider,route,slice_name', [('cbm', 'inherit', 'app.slice'),
                                                    ('gitnexus', 'capped', 'app-capped.slice')])
@pytest.mark.parametrize('mode,wall', [('fast', 3600), ('moderate', 3600), ('full', 14400)])
def test_default_policy_preserves_existing_effective_controls(tmp_path, monkeypatch, provider, route, slice_name, mode, wall):
    namespace, modules, manager, calls = prepared(tmp_path, monkeypatch)
    value = namespace['policy'](modules, provider, mode, manager, {})
    assert (value['route'], value['slice'], value['wall_seconds']) == (route, slice_name, wall)
    assert value['io_weight'] == '20' and value['cpu_quota'] == '200%'
    assert value['sibling_reserve_bytes'] == value['cache_reserve_bytes'] == 2 * 1024**3
    assert value['watchdog'] == dict(interval='15', warmup_interval='5', warmup_seconds='30',
        load_max='4', iowait_max='25', bad_samples='2', continuous_pause_max='600')
    assert calls == ([os.getpid()] if provider == 'cbm' else [])


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
def test_workload_route_needs_no_scope_probe(tmp_path, monkeypatch, provider):
    namespace, modules, manager, calls = prepared(tmp_path, monkeypatch)
    value = namespace['policy'](modules, provider, 'full', manager,
        {'CODE_INTEL_WORKLOAD_SLICE': '1', 'CODE_INTEL_INDEX_IO_WEIGHT': '100'})
    assert value['route'] == 'workload' and value['slice'] == 'genesis-workload.slice'
    assert value['io_weight'] == '100' and not calls


@pytest.mark.parametrize('provider', ['cbm', 'gitnexus'])
@pytest.mark.parametrize('field', ['CODE_INTEL_SIBLING_RESERVE_BYTES', 'CODE_INTEL_FILE_CACHE_RESERVE_BYTES'])
@pytest.mark.parametrize('amount', [0, 2**31 - 1, 2**31 + 1])
def test_environment_reserves_cannot_lower_the_existing_floor(tmp_path, monkeypatch, provider, field, amount):
    namespace, modules, manager, _ = prepared(tmp_path, monkeypatch)
    if amount < 2**31:
        with pytest.raises(ValueError):
            namespace['policy'](modules, provider, 'full', manager, {field: str(amount)})
    else:
        selected = namespace['policy'](modules, provider, 'full', manager, {field: str(amount)})
        key = 'sibling_reserve_bytes' if field == 'CODE_INTEL_SIBLING_RESERVE_BYTES' else 'cache_reserve_bytes'
        assert selected[key] == amount


@pytest.mark.parametrize('field,value', [('CODE_INTEL_WORKLOAD_SLICE', 'yes'),
    ('CODE_INTEL_INDEX_MEMORY_MAX', '4G'), ('CODE_INTEL_CBM_MEMORY_MAX', '16G'),
    ('CODE_INTEL_INDEX_IO_WEIGHT', '0'), ('CODE_INTEL_INDEX_CPU_QUOTA', 'unlimited'),
    ('CODE_INTEL_WATCHDOG_BAD_SAMPLES', '0'), ('CODE_INTEL_WATCHDOG_WALL_FULL', '0'),
    ('CODE_INTEL_WATCHDOG_INTERVAL', 'nan'), ('CODE_INTEL_SIBLING_RESERVE_BYTES', '-1'),
    ('CODE_INTEL_INDEX_DISABLE', '1')])
def test_invalid_or_disabled_policy_cannot_reach_reservation(tmp_path, monkeypatch, field, value):
    namespace, modules, manager, _ = prepared(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        namespace['policy'](modules, 'cbm', 'full', manager, {field: value})


def test_scope_control_environment_uses_full_reserved_inputs(tmp_path, monkeypatch):
    _, _, snapshot = case(tmp_path, monkeypatch, 'runner')
    modules = snapshot.assemble()
    value = runtime(snapshot, modules)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner.py'))
    operation = getattr(SimpleNamespace(**namespace), 'control_environment', None)
    assert callable(operation), 'actual scope receives the bound admission environment'
    unit = SimpleNamespace(systemctl_env=lambda: {'DBUS_SESSION_BUS_ADDRESS': 'synthetic',
                                                 'CODE_INTEL_CHILD_REFUSAL_MARKER': '/unused'})
    selected = operation(value['reservation'], value['policy'], unit)
    assert selected['CODE_INTEL_CHILD_CAP_BYTES'] == '8589934592'
    assert selected['CODE_INTEL_CHILD_SCOPE_UNIT'] == value['reservation']['unit']
    assert selected['CODE_INTEL_CHILD_RESERVE_BYTES'] == selected['CODE_INTEL_FILE_CACHE_RESERVE_BYTES'] == '2147483648'
    assert selected['CODE_INTEL_CHILD_REQUIRE_WORKLOAD_SLICE'] == '1'
    assert selected['DBUS_SESSION_BUS_ADDRESS'] == 'synthetic'
    assert 'CODE_INTEL_CHILD_REFUSAL_MARKER' not in selected


def runtime_case(queue, tmp_path, monkeypatch):
    root, source, snapshot = case(tmp_path, monkeypatch, 'runner')
    # Use ONE captured module universe, never the earlier setup importer.
    # case() selects a new scratch GENESIS_HOME; explicitly upgrade only it.
    for _, alias, _ in source.MODULES:
        monkeypatch.delitem(sys.modules, alias, raising=False)
    modules = dict(snapshot.assemble())
    queue = modules['queue']
    assert queue.database_path().is_relative_to(tmp_path)
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner_admission.py'))
    queue.write_marker(str(root), 'cbm', 'fast')
    claimed = queue.claim_native(queue.marker_hash(str(root)), provider='cbm')
    modules['queue'], modules['native'] = queue, queue._native
    config = dict(version=2, main=str(root), binary=str(root / 'synthetic-binary'),
        cache=str(root / 'cache'), runtime=str(root / 'runtime'), sentinel=str(root / 'disabled'), build=modules['worker'].BUILD)
    settings = root / 'settings.json'
    settings.write_text(json.dumps(config))
    streams = []

    @contextmanager
    def verified(path):
        # Pin verification itself has separate tests; this fixture never needs
        # the installed provider and permits no provider/version invocation.
        with open('/dev/null', 'rb') as stream:
            streams.append(stream)
            yield stream

    monkeypatch.setattr(modules['managed'], 'verified_binary', verified)
    monkeypatch.setattr(modules['managed'], 'lifecycle_lock', lambda **kwargs: nullcontext())
    monkeypatch.setattr(modules['unit'], 'systemctl_env', lambda: dict(PATH='/usr/bin:/bin',
        INVOCATION_ID='a' * 32, SYNTHETIC_RUNTIME_VALUE='fixture-hidden'))
    manager = SimpleNamespace(owner_slice=lambda pid: 'app.slice')
    selected = namespace['policy'](modules, 'cbm', 'fast', manager, {})
    return namespace, snapshot, modules, claimed, settings, selected, streams


def test_actual_self_and_closed_pin_handle_precede_first_reservation(queue, tmp_path, monkeypatch):
    namespace, snapshot, modules, claimed, settings, selected, streams = runtime_case(queue, tmp_path, monkeypatch)
    queue = modules['queue']
    original = queue.reserve_launch

    def reserve(context, value):
        assert streams and streams[-1].closed
        assert value['owner'] == queue._recovery.owner_identity()
        return original(context, value)

    monkeypatch.setattr(queue, 'reserve_launch', reserve)
    actual_owner = queue._recovery.owner_identity()
    bound = namespace['reserve_runtime'](snapshot, modules, claimed['context'], 'fast', str(settings), selected,
                                         actual_owner=actual_owner)
    value = bound.runtime_value
    assert value['reservation'] == queue.execution_status(claimed['context'])['launch_reservation']
    assert value['environment_recipe']['frozen']['SYNTHETIC_RUNTIME_VALUE'] == 'fixture-hidden'
    assert 'INVOCATION_ID' not in value['environment_recipe']['frozen']
    assert 'fixture-hidden' not in repr(bound.resume_command('reporter', '3'))
    with pytest.raises(ValueError):
        namespace['reserve_runtime'](snapshot, modules, claimed['context'], 'fast', str(settings), selected,
                                     actual_owner=actual_owner)


def test_lost_reservation_ack_cannot_resend_or_reassign_owner(queue, tmp_path, monkeypatch):
    namespace, snapshot, modules, claimed, settings, selected, _ = runtime_case(queue, tmp_path, monkeypatch)
    queue = modules['queue']
    original = queue.reserve_launch
    actual_owner = queue._recovery.owner_identity()

    def lost(context, value):
        assert original(context, value) == 'reserved'
        raise OSError('synthetic lost reservation acknowledgement')

    monkeypatch.setattr(queue, 'reserve_launch', lost)
    with pytest.raises(OSError):
        namespace['reserve_runtime'](snapshot, modules, claimed['context'], 'fast', str(settings), selected,
                                     actual_owner=actual_owner)
    reserved = queue.execution_status(claimed['context'])['launch_reservation']
    monkeypatch.setattr(queue, 'reserve_launch', original)
    with pytest.raises(ValueError):
        namespace['reserve_runtime'](snapshot, modules, claimed['context'], 'fast', str(settings), selected,
                                     actual_owner=actual_owner)
    assert queue.execution_status(claimed['context'])['launch_reservation'] == reserved
    assert queue.execution_status(claimed['context'])['scope_registration'] is None


def test_metadata_prefix_never_admits_selected_provenance_through_ordinary_owner(queue, tmp_path, monkeypatch):
    namespace, snapshot, modules, claimed, settings, selected, streams = runtime_case(queue, tmp_path, monkeypatch)
    queue = modules['queue']
    with closing(queue._connect()) as db, queue._transaction(db):
        db.execute("UPDATE executions SET nonce_origin='selected-recovery' WHERE execution_seq=?",
                   (claimed['context']['execution_seq'],))
    before = queue.execution_status(claimed['context'])
    with pytest.raises(ValueError, match='selected runtime'):
        namespace['reserve_runtime'](snapshot, modules, claimed['context'], 'fast', str(settings), selected,
                                     actual_owner=queue._recovery.owner_identity())
    assert not streams
    assert queue.execution_status(claimed['context']) == before


@pytest.mark.parametrize('field', ['pid', 'start_ticks', 'boot_id'])
@pytest.mark.parametrize('seam', ['argument', 'before-cas'])
def test_actual_owner_snapshot_cannot_be_replaced_before_reservation(queue, tmp_path, monkeypatch, field, seam):
    namespace, snapshot, modules, claimed, settings, selected, streams = runtime_case(queue, tmp_path, monkeypatch)
    queue = modules['queue']
    owner = queue._recovery.owner_identity()
    changed = {**owner, field: (owner[field] + 1 if field != 'boot_id'
                              else owner[field][:-1] + ('0' if owner[field][-1] != '0' else '1'))}
    original = modules['queued'].runtime_inputs

    def drift(bound, modules):
        value = original(bound, modules)
        monkeypatch.setattr(queue._recovery, 'owner_identity', lambda: changed)
        return value

    if seam == 'before-cas':
        monkeypatch.setattr(modules['queued'], 'runtime_inputs', drift)
    with pytest.raises(ValueError, match='owner'):
        namespace['reserve_runtime'](snapshot, modules, claimed['context'], 'fast', str(settings), selected,
                                     actual_owner=changed if seam == 'argument' else owner)
    assert queue.execution_status(claimed['context'])['launch_reservation'] is None
    assert bool(streams) is (seam == 'before-cas')
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('field', ['pid', 'start_ticks', 'boot_id'])
def test_policy_lookup_is_bracketed_by_original_owner_birth(queue, tmp_path, monkeypatch, field):
    namespace, snapshot, modules, claimed, settings, selected, _ = runtime_case(queue, tmp_path, monkeypatch)
    queue = modules['queue']
    original_owner = queue._recovery.owner_identity()
    changed = {**original_owner, field: (original_owner[field] + 1 if field != 'boot_id'
                                       else original_owner[field][:-1] + ('0' if original_owner[field][-1] != '0' else '1'))}
    calls = []

    def owner_slice(pid):
        assert pid == original_owner['pid']
        calls.append('lookup')
        monkeypatch.setattr(queue._recovery, 'owner_identity', lambda: changed)
        return 'app.slice'

    def select_policy(modules, provider, mode, manager, environment):
        # Isolate the main's identity bracket; policy/locks have separate tests.
        assert manager.owner_slice(os.getpid()) == 'app.slice'
        return selected

    globals_ = namespace['main'].__globals__
    monkeypatch.setitem(globals_, 'repository_lock', lambda *args: nullcontext())
    monkeypatch.setitem(globals_, 'policy', select_policy)
    monkeypatch.setattr(modules['scope'], 'ScopeManager', lambda *args, **kwargs:
                        SimpleNamespace(owner_slice=owner_slice))
    monkeypatch.setitem(globals_, 'reserve_runtime', lambda *args, **kwargs: pytest.fail('changed birth reached CAS'))
    assert namespace['main'](snapshot, modules, json.dumps(claimed['context']), 'fast', str(settings)) == 76
    assert calls == ['lookup']
    assert queue.execution_status(claimed['context'])['launch_reservation'] is None


@pytest.mark.parametrize('fault', [None, 'runtime', 'random', 'signal'])
def test_actual_producer_enables_runtime_validation_without_changing_generic_lookup(
        queue, tmp_path, monkeypatch, fault):
    from tests.test_scripts.test_code_intel_scope_manager import CLAIM, UNIT, variant
    from tests.test_scripts.test_code_intel_scope_runtime import runtime_bus
    from tests.test_scripts.test_index_marker_execution import _facts

    namespace, snapshot, modules, claimed, settings, selected, _ = runtime_case(queue, tmp_path, monkeypatch)
    constructor = modules['scope'].ScopeManager
    managers, allowed, refused, census = [], [], [], []

    def observed_constructor(bus_call, property_value, **kwargs):
        bus = runtime_bus(selected['wall_seconds'])
        if fault is not None:
            name, value = {'runtime': ('RuntimeMaxUSec', variant('t', 2**64 - 1)),
                'random': ('RuntimeRandomizedExtraUSec', variant('t', 1)),
                'signal': ('FinalKillSignal', variant('i', 15))}[fault]
            bus.scope[name] = value
        def call(path, interface, method, *args, timeout):
            if method == 'ListUnitsByPatterns':
                assert args == ('asas', '0', '1', 'code-intel-*.scope')
                census.append(True)  # This fixture's namespace has no old batch.
                return variant('a(ssssssouso)', [[]])
            return bus(path, interface, method, *args, timeout=timeout)
        operation = constructor(call, property_value,
            **{**kwargs, 'deadline': 1, 'clock': lambda: bus.now})
        managers.append(operation)
        return operation

    def physical_owner(modules, context, reserved, probe, manager):
        def run(bound):
            # Caller sensitivity, not native qualification: execute the real
            # manager contract selected by the actual producer main.
            try:
                manager.registration(UNIT, _facts(CLAIM), wall_seconds=selected['wall_seconds'])
            except ValueError:
                refused.append(True)
                return None
            allowed.append(True)
            return None
        return SimpleNamespace(run=run)

    globals_ = namespace['main'].__globals__
    monkeypatch.setitem(globals_, 'repository_lock', lambda *args: nullcontext())
    monkeypatch.setitem(globals_, 'policy', lambda *args: selected)
    monkeypatch.setattr(modules['scope'], 'ScopeManager', observed_constructor)
    monkeypatch.setattr(modules['owner'], 'PhysicalOwner', physical_owner)
    assert namespace['main'](snapshot, modules, json.dumps(claimed['context']), 'fast', str(settings)) == 76
    assert len(managers) == 2
    assert census == [True]
    assert managers[0].require_runtime_limit is False and managers[1].require_runtime_limit is True
    assert bool(allowed) is (fault is None) and bool(refused) is (fault is not None)
    state = modules['queue'].execution_status(claimed['context'])
    assert state['launch_reservation'] is not None
    assert state['native_intent'] is state['control_closure'] is None
