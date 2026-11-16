"""Effective runtime policy is strict for initial owners, optional for readers."""
from copy import deepcopy

import pytest

from tests.test_scripts.test_code_intel_scope_manager import CLAIM, UNIT, Bus, manager, variant
from tests.test_scripts.test_index_marker_execution import _facts


def runtime_bus(wall=3600):
    bus = Bus()
    bus.scope.update(RuntimeMaxUSec=variant('t', wall * 1_000_000),
                     RuntimeRandomizedExtraUSec=variant('t', 0), FinalKillSignal=variant('i', 9))
    return bus


def strict(bus, **kwargs):
    generic = manager(bus)
    return type(generic)(bus, generic.property, deadline=1, clock=lambda: bus.now,
                         require_runtime_limit=kwargs.get('require_runtime_limit', True))


@pytest.mark.parametrize('wall', [3600, 14400])
def test_effective_runtime_policy_uses_the_existing_snapshot_without_new_header_fields(wall):
    bus, facts = runtime_bus(wall), _facts(CLAIM)
    result = strict(bus).registration(UNIT, facts, wall_seconds=wall)
    assert result == dict(unit=UNIT, invocation_id='01' * 16, facts=facts, stop_usec=90_000_000)
    assert [call[2] for call in bus.calls].count('GetAll') == 3
    assert len(bus.calls) == 7


@pytest.mark.parametrize('name,signature,values', [
    ('RuntimeMaxUSec', 't', [None, 0, -1, True, '3600000000', 3600000000.0,
                           3600000001, 2**64 - 1, 2**64]),
    ('RuntimeRandomizedExtraUSec', 't', [None, 1, -1, True, '0', 0.0, 2**64 - 1, 2**64]),
    ('FinalKillSignal', 'i', [None, 0, 15, -1, True, '9', 9.0, 2**31]),
])
def test_missing_malformed_or_different_runtime_policy_refuses(name, signature, values):
    for value in values:
        bus = runtime_bus()
        if value is None:
            del bus.scope[name]
        else:
            bus.scope[name] = variant(signature, value)
        with pytest.raises(ValueError):
            strict(bus).registration(UNIT, _facts(CLAIM), wall_seconds=3600)


@pytest.mark.parametrize('name,signature,value', [
    ('RuntimeMaxUSec', 'i', 3600000000), ('RuntimeRandomizedExtraUSec', 'i', 0),
    ('FinalKillSignal', 't', 9),
])
def test_runtime_property_bus_signatures_are_not_coerced(name, signature, value):
    bus = runtime_bus()
    bus.scope[name] = variant(signature, value)
    with pytest.raises(ValueError):
        strict(bus).registration(UNIT, _facts(CLAIM), wall_seconds=3600)


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_runtime_requirement_is_a_boolean_constructor_option(value):
    with pytest.raises(ValueError):
        strict(Bus(), require_runtime_limit=value)


@pytest.mark.parametrize('present', [False, True])
def test_generic_reader_preserves_legacy_contract_even_with_unrequested_runtime_values(present):
    bus = Bus()
    if present:
        bus.scope.update(RuntimeMaxUSec=variant('t', 2**64 - 1),
                         RuntimeRandomizedExtraUSec=variant('t', 100), FinalKillSignal=variant('i', 15))
    facts = _facts(CLAIM)
    assert manager(bus).registration(UNIT, facts, wall_seconds=3600)['facts'] == facts


@pytest.mark.parametrize('name,bad', [('RuntimeMaxUSec', variant('t', 2**64 - 1)),
    ('RuntimeRandomizedExtraUSec', variant('t', 1)), ('FinalKillSignal', variant('i', 15))])
def test_original_scope_recheck_keeps_the_strict_runtime_requirement(name, bad):
    from types import SimpleNamespace

    bus, facts = runtime_bus(), _facts(CLAIM)
    operation = strict(bus)
    registration = operation.registration(UNIT, facts, wall_seconds=3600)
    bus.now = 0
    bus.scope[name] = deepcopy(bad)
    proof = SimpleNamespace(_facts=lambda value: True, _complete=lambda *args: None)
    with pytest.raises(ValueError):
        operation.original(registration, facts, object_path='/org/freedesktop/systemd1/unit/owned',
                           wall_seconds=3600, proof=proof)
