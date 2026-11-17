"""An exited reporter cannot supply GetUnitByPID for original-subtree cleanup."""
import runpy
from copy import deepcopy
from functools import partial
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import SCRIPT
from tests.test_scripts.test_code_intel_scope_manager import CLAIM, UNIT, Bus, manager, variant
from tests.test_scripts.test_index_marker_execution import _facts

PATH = '/org/freedesktop/systemd1/unit/owned'


def original(bus=None):
    bus = Bus() if bus is None else bus
    operation = manager(bus)
    method = getattr(operation, 'original', None)
    assert callable(method), 'original manager observation must not depend on a live reporter PID'
    registration = dict(unit=UNIT, invocation_id='01' * 16, facts=_facts(CLAIM), stop_usec=90_000_000)
    return bus, operation, partial(method, proof=SimpleNamespace(**runpy.run_path(str(SCRIPT)))), registration


def test_original_scope_can_be_observed_without_looking_up_an_exited_pid():
    bus, operation, method, registration = original()
    assert method(registration, registration['facts'], object_path=PATH, wall_seconds=3600) == registration
    assert [call[2] for call in bus.calls].count('GetUnit') == 2
    assert all(call[2] not in ('GetUnitByPID', 'LoadUnit', 'StopUnit') for call in bus.calls)


@pytest.mark.parametrize('fault', ['object-path', 'invocation', 'stop', 'cap', 'swap', 'slice', 'group', 'kill-mode', 'sigkill'])
def test_replaced_scope_or_weakened_policy_is_not_original(fault):
    bus, operation, method, registration = original()
    expected_path = PATH
    if fault == 'object-path':
        expected_path += '_foreign'
    elif fault == 'invocation':
        bus.unit['InvocationID'] = variant('ay', [2] * 16)
    else:
        name, signature, value = {
            'stop': ('TimeoutStopUSec', 't', 80_000_000),
            'cap': ('MemoryMax', 't', 1), 'swap': ('MemorySwapMax', 't', 1),
            'slice': ('Slice', 's', 'foreign.slice'), 'group': ('ControlGroup', 's', '/foreign.scope'),
            'kill-mode': ('KillMode', 's', 'process'), 'sigkill': ('SendSIGKILL', 'b', False),
        }[fault]
        bus.scope[name] = variant(signature, value)
    with pytest.raises(ValueError):
        method(registration, registration['facts'], object_path=expected_path, wall_seconds=3600)
    assert not any(call[2] in ('GetUnitByPID', 'StopUnit') for call in bus.calls)


@pytest.mark.parametrize('drift', ['lookup', 'invocation'])
def test_original_identity_is_bracketed_during_read(drift):
    bus, operation, method, registration = original()
    bus.drift = drift
    with pytest.raises(ValueError):
        method(registration, registration['facts'], object_path=PATH, wall_seconds=3600)


def test_original_scope_returns_fresh_counters_without_mutating_its_baseline():
    bus, operation, method, registration = original()
    latest = deepcopy(registration['facts'])
    latest['events']['high'] += 1
    result = method(registration, latest, object_path=PATH, wall_seconds=3600)
    assert result['facts'] == latest and result['facts'] is not latest
    assert registration['facts']['events']['high'] == 0


@pytest.mark.parametrize('fault', ['leaf', 'root', 'mount', 'reporter', 'boot', 'counter-reset'])
def test_original_kernel_lifetime_cannot_be_replaced_by_a_matching_manager(fault):
    bus, operation, method, registration = original()
    latest = deepcopy(registration['facts'])
    if fault in ('leaf', 'root'):
        latest[fault]['inode'] += 1
    elif fault == 'mount':
        latest['mounts_sha256'] = '2' * 64
    elif fault == 'reporter':
        latest['reporter']['start_ticks'] += 1
    elif fault == 'boot':
        latest['reporter']['boot_id'] = '22222222-2222-4222-8222-222222222222'
    else:
        registration['facts']['events']['high'] = 1
    with pytest.raises(ValueError):
        method(registration, latest, object_path=PATH, wall_seconds=3600)
    assert not bus.calls, 'kernel mismatch must refuse before querying a possibly replaced unit'
