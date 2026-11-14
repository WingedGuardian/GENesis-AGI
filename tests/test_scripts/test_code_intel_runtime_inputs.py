"""Runtime roles validate source, policy and provider inputs before queue access."""
import pytest

from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def inputs(tmp_path, monkeypatch, change=None):
    root, source, snapshot = case(tmp_path, monkeypatch, 'runner')
    modules = snapshot.assemble()
    value = runtime(snapshot, modules)
    if change:
        change(value)
    snapshot = snapshot.with_runtime(value)
    operation = getattr(modules['queued'], 'runtime_inputs', None)
    assert callable(operation), 'runtime executable roles require complete cross-bound inputs'
    return operation, snapshot, modules, value


def test_valid_runtime_is_cross_bound_without_queue_access(tmp_path, monkeypatch):
    operation, snapshot, modules, value = inputs(tmp_path, monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail('runtime decoding must not open the queue or launch anything')
    monkeypatch.setattr(modules['queue'], 'execution_status', forbidden)
    checked, environment = operation(snapshot, modules)
    assert checked == value and environment.recipe == value['environment_recipe']
    checked['policy']['watchdog']['interval'] = '999'
    assert snapshot.runtime_value == value


@pytest.mark.parametrize('fault', ['source-commit', 'source-files', 'provider-pin', 'provider-version',
                                  'policy-wall', 'policy-route', 'policy-slice', 'partial-policy',
                                  'options-extra', 'relative-config', 'nul-config', 'persistence',
                                  'environment', 'context-hash', 'reservation-unit'])
def test_cross_bound_runtime_mismatch_refuses_before_effects(tmp_path, monkeypatch, fault):
    def change(value):
        reservation, policy, options = value['reservation'], value['policy'], value['provider_options']
        if fault == 'source-commit':
            reservation['source']['commit'] = '2' * 40
        elif fault == 'source-files':
            reservation['source']['files'] = {'scripts/foreign.py': '2' * 64}
        elif fault == 'provider-pin':
            reservation['source']['provider_digest'] = '2' * 64
        elif fault == 'provider-version':
            reservation['source']['provider_version'] = 'foreign'
        elif fault.startswith('policy-'):
            key = fault[len('policy-'):]
            policy[key if key != 'wall' else 'wall_seconds'] = {
                'wall': 7200, 'route': 'inherit', 'slice': 'foreign.slice'}[key]
        elif fault == 'partial-policy':
            policy.pop('watchdog')
        elif fault == 'options-extra':
            options['command'] = ['/bin/false']
        elif fault == 'relative-config':
            options['managed_config'] = 'settings.json'
        elif fault == 'nul-config':
            options['managed_config'] = '/fixture/invalid\0settings'
        elif fault == 'persistence':
            options['persistence'] = True
        elif fault == 'environment':
            value['environment_recipe']['frozen']['INVOCATION_ID'] = '1' * 32
        elif fault == 'context-hash':
            value['context']['hash'] = '2' * 16
        else:
            reservation['unit'] = 'foreign'
    operation, snapshot, modules, value = inputs(tmp_path, monkeypatch, change)
    with pytest.raises(ValueError):
        operation(snapshot, modules)


def test_source_only_capsule_cannot_acquire_runtime_inputs(tmp_path, monkeypatch):
    operation, snapshot, modules, value = inputs(tmp_path, monkeypatch)
    source_only = type(snapshot)(snapshot.root, snapshot.profile, snapshot.commit, snapshot.buffers)
    with pytest.raises(ValueError, match='runtime'):
        operation(source_only, modules)
