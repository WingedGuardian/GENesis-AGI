"""Typed existing-unit observation only; no manager mutation or scope launch."""
import runpy
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_index_marker_execution import _facts, _reservation

ROOT = Path(__file__).resolve().parents[2]
CLAIM = {'context': {'hash': '0123456789abcdef', 'provider': 'cbm', 'claim_id': '1' * 32}, 'mode': 'fast'}
UNIT = _reservation(CLAIM)['unit']


def variant(signature, value):
    return {'type': signature, 'data': value}


class Bus:
    def __init__(self):
        self.now = 0.0
        self.calls = []
        self.unit = {'Id': variant('s', UNIT + '.scope'), 'LoadState': variant('s', 'loaded'),
                     'InvocationID': variant('ay', [1] * 16)}
        self.scope = {'Slice': variant('s', 'genesis-workload.slice'),
                      'ControlGroup': variant('s', '/user.slice/genesis-workload.slice/' + UNIT + '.scope'),
                      'ControlGroupId': variant('t', 3),
                      'TimeoutStopUSec': variant('t', 90_000_000),
                      'MemoryMax': variant('t', 8 * 1024**3), 'MemorySwapMax': variant('t', 0),
                      'KillMode': variant('s', 'control-group'), 'SendSIGKILL': variant('b', True)}
        self.unit_reads = 0
        self.lookup_reads = 0
        self.drift = None

    def __call__(self, path, interface, method, *args, timeout):
        assert timeout > 0 and timeout <= 1.0 - self.now + 1e-9
        self.calls.append((path, interface, method, args, timeout))
        self.now += 0.1
        if method in {'GetUnit', 'GetUnitByPID'}:
            self.lookup_reads += 1
            if (self.drift == 'lookup' and method == 'GetUnit'
                    and [call[2] for call in self.calls].count('GetUnit') > 1):
                return variant('o', ['/org/freedesktop/systemd1/unit/replacement'])
            assert args == ('s', UNIT + '.scope') if method == 'GetUnit' else args == ('u', '123')
            return variant('o', ['/org/freedesktop/systemd1/unit/owned'])
        assert method == 'GetAll' and interface == 'org.freedesktop.DBus.Properties'
        assert path == '/org/freedesktop/systemd1/unit/owned'
        if args == ('s', 'org.freedesktop.systemd1.Unit'):
            self.unit_reads += 1
            data = deepcopy(self.unit)
            if self.drift == 'invocation' and self.unit_reads > 1:
                data['InvocationID'] = variant('ay', [2] * 16)
        else:
            assert args in [('s', 'org.freedesktop.systemd1.Scope'), ('s', 'org.freedesktop.systemd1.Service')]
            data = deepcopy(self.scope)
        return variant('a{sv}', [data])


def manager(bus):
    namespace = runpy.run_path(str(ROOT / 'scripts/lib/code_intel_scope.py'))
    constructor = namespace.get('ScopeManager')
    assert callable(constructor), 'Q3b needs typed existing-unit identity, not LoadUnit'
    old_path = sys.path[:]
    try:
        sys.path.insert(0, str(ROOT / 'scripts/lib'))
        reader = runpy.run_path(str(ROOT / 'scripts/lib/codebase_managed_unit.py'))['typed_property']
    finally:
        sys.path[:] = old_path
    return constructor(bus, reader, deadline=1.0, clock=lambda: bus.now)


def test_registration_uses_getunit_and_bounded_typed_properties():
    bus = Bus()
    facts = _facts(CLAIM)
    result = manager(bus).registration(UNIT, facts, wall_seconds=3600)
    assert result == {'unit': UNIT, 'invocation_id': '01' * 16,
                      'facts': facts, 'stop_usec': 90_000_000}
    assert [call[2] for call in bus.calls].count('GetUnit') == 2
    assert [call[2] for call in bus.calls].count('GetAll') == 3
    assert [call[2] for call in bus.calls].count('GetUnitByPID') == 2
    assert all(call[2] not in {'LoadUnit', 'StartTransientUnit', 'StopUnit'} for call in bus.calls)
    assert [call[4] for call in bus.calls] == pytest.approx([1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4])


@pytest.mark.parametrize('native_id', [3, 2**40 + 3])
def test_native_group_identity_survives_different_visible_path_prefixes(native_id):
    bus = Bus()
    facts = _facts(CLAIM)
    facts['leaf']['inode'] = native_id
    bus.scope['ControlGroupId'] = variant('t', native_id)
    bus.scope['ControlGroup'] = variant('s', '/manager-prefix/genesis-workload.slice/' + UNIT + '.scope')
    assert manager(bus).registration(UNIT, facts, wall_seconds=3600)['facts'] == facts


@pytest.mark.parametrize('value', [None, 0, -1, True, '3', 3.0, 4, 2**40 + 3, 2**64])
def test_missing_malformed_or_different_native_group_identity_refuses(value):
    bus = Bus()
    if value is None:
        del bus.scope['ControlGroupId']
    else:
        bus.scope['ControlGroupId'] = variant('t', value)
    with pytest.raises(ValueError):
        manager(bus).registration(UNIT, _facts(CLAIM), wall_seconds=3600)


def test_enclosing_unit_does_not_authorize_a_delegated_leaf_with_repeated_names():
    bus = Bus()
    facts = _facts(CLAIM)
    original = deepcopy(facts['leaf'])
    nested = Path(original['path']) / 'genesis-workload.slice' / (UNIT + '.scope')
    facts['leaf'].update(path=str(nested), inode=4)
    facts['membership'] = '0::/' + str(nested.relative_to(facts['root']['path'])) + '\n'
    counters = facts['events']
    facts['ancestors'] = [
        {'identity': {'path': str(nested.parent), 'device': 7, 'inode': 5},
         'events': deepcopy(counters), 'error': None},
        {'identity': original, 'events': deepcopy(counters), 'error': None},
        *facts['ancestors'],
    ]
    # Both basename comparisons and both enclosing-unit lookups still match.
    # The actual deeper kernel identity differs from the manager's outer scope.
    with pytest.raises(ValueError):
        manager(bus).registration(UNIT, facts, wall_seconds=3600)


def test_native_group_id_requires_the_captured_cgroup_filesystem():
    facts = _facts(CLAIM)
    facts['leaf']['device'] += 1
    with pytest.raises(ValueError):
        manager(Bus()).registration(UNIT, facts, wall_seconds=3600)


def test_truncated_kernel_inode_platform_cannot_claim_full_native_id(monkeypatch):
    operation = manager(Bus())
    monkeypatch.setitem(operation.registration.__globals__, 'sys', SimpleNamespace(maxsize=2**31 - 1))
    with pytest.raises(ValueError):
        operation.registration(UNIT, _facts(CLAIM), wall_seconds=3600)


@pytest.mark.parametrize('fault', ['id', 'load', 'invocation-type', 'invocation-size', 'invocation-bool',
                                  'slice', 'group', 'relative-group', 'traversal-group',
    'stop-zero', 'stop-infinite', 'stop-bool', 'native-id-type', 'cap', 'swap',
                                  'kill-mode', 'no-sigkill', 'sigkill-type'])
def test_unproven_scope_identity_or_stop_policy_refuses(fault):
    bus = Bus()
    operation = manager(bus)
    target, name, value = {
        'id': (bus.unit, 'Id', variant('s', 'different.scope')),
        'load': (bus.unit, 'LoadState', variant('s', 'not-found')),
        'invocation-type': (bus.unit, 'InvocationID', variant('s', '01' * 16)),
        'invocation-size': (bus.unit, 'InvocationID', variant('ay', [1] * 15)),
        'invocation-bool': (bus.unit, 'InvocationID', variant('ay', [True] * 16)),
        'slice': (bus.scope, 'Slice', variant('s', 'different.slice')),
        'group': (bus.scope, 'ControlGroup', variant('s', '/genesis-workload.slice/different.scope')),
        'relative-group': (bus.scope, 'ControlGroup', variant('s', 'genesis-workload.slice/' + UNIT + '.scope')),
        'traversal-group': (bus.scope, 'ControlGroup', variant('s', '/one/../genesis-workload.slice/' + UNIT + '.scope')),
        'stop-zero': (bus.scope, 'TimeoutStopUSec', variant('t', 0)),
        'stop-infinite': (bus.scope, 'TimeoutStopUSec', variant('t', 2**64 - 1)),
        'stop-bool': (bus.scope, 'TimeoutStopUSec', variant('t', True)),
        'native-id-type': (bus.scope, 'ControlGroupId', variant('u', 3)),
        'cap': (bus.scope, 'MemoryMax', variant('t', 2 * 1024**3)),
        'swap': (bus.scope, 'MemorySwapMax', variant('t', 1)),
        'kill-mode': (bus.scope, 'KillMode', variant('s', 'process')),
        'no-sigkill': (bus.scope, 'SendSIGKILL', variant('b', False)),
        'sigkill-type': (bus.scope, 'SendSIGKILL', variant('b', 1)),
    }[fault]
    target[name] = value
    with pytest.raises(ValueError):
        operation.registration(UNIT, _facts(CLAIM), wall_seconds=3600)


@pytest.mark.parametrize('drift', ['invocation', 'lookup'])
def test_manager_identity_is_rechecked_around_property_reads(drift):
    bus = Bus()
    bus.drift = drift
    with pytest.raises(ValueError):
        manager(bus).registration(UNIT, _facts(CLAIM), wall_seconds=3600)


def test_two_stop_phases_must_fit_the_effective_existing_wall_cap():
    bus = Bus()
    with pytest.raises(ValueError):
        manager(bus).registration(UNIT, _facts(CLAIM), wall_seconds=100)


@pytest.mark.parametrize('kind', ['Scope', 'Service'])
def test_inherited_slice_is_read_from_the_actual_owner_type(kind):
    bus = Bus()
    bus.unit['Id'] = variant('s', 'owned.' + kind.lower())
    assert manager(bus).owner_slice(123) == 'genesis-workload.slice'
    assert any(call[3] == ('s', 'org.freedesktop.systemd1.' + kind) for call in bus.calls)
    assert not any(call[2] == 'GetUnit' for call in bus.calls)


def test_each_call_uses_one_remaining_deadline():
    bus = Bus()
    operation = manager(bus)
    bus.now = 1.0
    with pytest.raises(ValueError):
        operation.registration(UNIT, _facts(CLAIM), wall_seconds=3600)
    assert bus.calls == []


def test_last_reply_arriving_after_deadline_cannot_publish_a_registration():
    class SlowLastReply(Bus):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            if [call[2] for call in self.calls].count('GetUnitByPID') == 2:
                self.now = 1.1
            return result

    bus = SlowLastReply()
    with pytest.raises(ValueError):
        manager(bus).registration(UNIT, _facts(CLAIM), wall_seconds=3600)


@pytest.mark.parametrize('fault', ['foreign', 'missing', 'changed', 'late'])
def test_named_scope_must_be_the_actual_reporters_unit(fault):
    class ReporterBus(Bus):
        pid_reads = 0

        def __call__(self, path, interface, method, *args, **kwargs):
            result = super().__call__(path, interface, method, *args, **kwargs)
            if method == 'GetUnitByPID':
                self.pid_reads += 1
                if fault == 'foreign' or (fault == 'changed' and self.pid_reads > 1):
                    return variant('o', ['/org/freedesktop/systemd1/unit/foreign'])
                if fault == 'missing':
                    return variant('o', [])
                if fault == 'late':
                    self.now = 1.1
            return result

    bus = ReporterBus()
    with pytest.raises(ValueError):
        manager(bus).registration(UNIT, _facts(CLAIM), wall_seconds=3600)
