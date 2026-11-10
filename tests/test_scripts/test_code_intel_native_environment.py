"""Freeze native values while retaining real scope-control provenance."""

import runpy
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_index_marker_legs import queue as queue

ROOT = Path(__file__).resolve().parents[2]


def recipe():
    return dict(frozen={'PATH': '/usr/bin:/bin', 'SYNTHETIC_VALUE': 'private-fixture-value',
                        'CBM_CACHE_DIR': '/fixture/cache', 'CBM_RUNTIME_DIR': '/fixture/runtime',
                        'CBM_ALLOWED_ROOT': '/fixture/repository'},
                registration_slots={'INVOCATION_ID': 'scope.invocation_id'})


def construct(queue, value):
    namespace = runpy.run_path(str(ROOT / 'scripts/lib/code_intel_queued_worker.py'))
    constructor = namespace.get('NativeEnvironment')
    assert callable(constructor), 'queued native input needs the explicit registered-environment recipe'
    return constructor(value, queue._native)


def test_recipe_resolves_only_its_registered_invocation_without_ambient_rebuild(queue):
    value = recipe()
    original = deepcopy(value)
    plan = construct(queue, value)
    value['frozen']['PATH'] = '/changed'
    actual = dict(INVOCATION_ID='1' * 32, TRANSPORT_ONLY='not a native input')
    resolved = plan.resolve('1' * 32, actual)
    assert dict(resolved) == {**original['frozen'], 'INVOCATION_ID': '1' * 32}
    assert actual == dict(INVOCATION_ID='1' * 32, TRANSPORT_ONLY='not a native input')
    assert dict(plan.frozen) == original['frozen'] and plan.recipe == original
    exposed = plan.recipe
    exposed['frozen']['PATH'] = '/also-changed'
    assert plan.recipe == original
    assert 'private-fixture-value' not in repr(plan)
    with pytest.raises(TypeError):
        resolved['PATH'] = '/unbound'
    with pytest.raises(TypeError):
        plan.frozen['PATH'] = '/unbound'
    with pytest.raises(AttributeError):
        plan.frozen = {'PATH': '/unbound'}


@pytest.mark.parametrize('fault', ['not-object', 'extra', 'missing-frozen', 'missing-slots',
                                  'frozen-type', 'slots-type', 'slots-empty', 'slots-extra',
                                  'slots-other-key', 'slots-other-source', 'key-type', 'key-empty',
                                  'key-equals', 'key-nul', 'value-type', 'value-nul', 'frozen-invocation'])
def test_recipe_rejects_ambiguous_or_unexecutable_environment(queue, fault):
    value = recipe()
    if fault == 'not-object':
        value = []
    elif fault == 'extra':
        value['unknown'] = {}
    elif fault.startswith('missing-'):
        value.pop({'missing-frozen': 'frozen', 'missing-slots': 'registration_slots'}[fault])
    elif fault == 'frozen-type':
        value['frozen'] = []
    elif fault == 'slots-type':
        value['registration_slots'] = []
    elif fault == 'slots-empty':
        value['registration_slots'] = {}
    elif fault == 'slots-extra':
        value['registration_slots']['OTHER'] = 'scope.invocation_id'
    elif fault == 'slots-other-key':
        value['registration_slots'] = {'OTHER': 'scope.invocation_id'}
    elif fault == 'slots-other-source':
        value['registration_slots']['INVOCATION_ID'] = 'ambient'
    else:
        key, entry = {
            'key-type': (1, 'value'), 'key-empty': ('', 'value'),
            'key-equals': ('BAD=KEY', 'value'), 'key-nul': ('BAD\0KEY', 'value'),
            'value-type': ('BAD', 1), 'value-nul': ('BAD', 'bad\0value'),
            'frozen-invocation': ('INVOCATION_ID', '1' * 32),
        }[fault]
        value['frozen'][key] = entry
    with pytest.raises(ValueError, match='environment|recipe|registration'):
        construct(queue, value)


@pytest.mark.parametrize('invocation,actual', [
    ('1' * 32, {}), ('1' * 32, {'INVOCATION_ID': '2' * 32}),
    ('1' * 32, {'INVOCATION_ID': None}), ('1' * 32, {'INVOCATION_ID': 1}),
    ('1' * 31, {'INVOCATION_ID': '1' * 31}), ('A' * 32, {'INVOCATION_ID': 'A' * 32}),
    (True, {'INVOCATION_ID': True}), (None, {'INVOCATION_ID': None}),
])
def test_resolution_requires_the_actual_registered_scope_provenance(queue, invocation, actual):
    plan = construct(queue, recipe())
    with pytest.raises(ValueError, match='invocation'):
        plan.resolve(invocation, actual)
