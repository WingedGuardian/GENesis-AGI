"""Closed GN capsules bind the actual Node, package, environment and policy."""
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import adapter
from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def frozen(selection):
    snapshot, modules, environment, cwd, activity, _, _ = selection
    namespace = adapter()
    operation = namespace.get('freeze')
    assert callable(operation), 'GN reservation needs complete frozen runtime inputs'
    value = runtime(snapshot, modules)
    context, reserved, policy = (value[k] for k in ('context', 'reservation', 'policy'))
    context['provider'] = 'gitnexus'
    reserved['unit'] = modules['queue']._native.expected_unit(context)
    reserved['route'] = policy['route'] = 'capped'
    reserved['slice'] = policy['slice'] = 'app-capped.slice'
    options, recipe, source = operation(snapshot, modules, context, reserved, policy,
                                         environment, cwd=cwd)
    reserved['source'] = source
    value.update(provider_options=options, environment_recipe=recipe)
    assert not activity.exists()
    return snapshot, modules, value, namespace


def test_captured_inventory_includes_the_real_gn_adapter(selection):
    snapshot = selection[0]
    assert 'scripts/lib/code_intel_gitnexus.py' in snapshot.files
    assert 'gitnexus' in selection[1]


def test_gn_runtime_uses_actual_node_digest_and_complete_closed_inputs(selection):
    snapshot, modules, value, _ = frozen(selection)
    bound = snapshot.with_runtime(value)
    decoded, environment = modules['queued'].runtime_inputs(bound, modules)
    source = value['reservation']['source']
    assert decoded == value
    assert source['provider_digest'] == value['provider_options']['node']['sha256']
    assert source['provider_digest'] != value['provider_options']['entry']['sha256']
    assert source['provider_version'] == 'gitnexus@1.6.12'
    assert source['files'] == dict(snapshot.files) and source['commit'] == snapshot.commit
    assert environment.frozen['PATH'].startswith(str(selection[-1].parent)+':')
    assert environment.recipe == value['environment_recipe']


def test_runtime_decoder_does_not_open_provider_files_or_queue(selection, monkeypatch):
    snapshot, modules, value, _ = frozen(selection)
    bound = snapshot.with_runtime(value)
    def forbidden(*args, **kwargs):
        raise AssertionError('runtime decoding is not provider preparation or queue admission')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(modules['queue'], '_connect', forbidden)
    decoded, _ = modules['queued'].runtime_inputs(bound, modules)
    assert decoded == value


@pytest.mark.parametrize('change', ['node_digest', 'entry_digest', 'package_digest', 'boolean_inode',
    'relative_entry', 'extra_option', 'missing_package', 'version', 'path', 'environment', 'policy', 'mode'])
def test_gn_capsule_rejects_substituted_or_malformed_binding(selection, change):
    snapshot, modules, value, _ = frozen(selection)
    altered = deepcopy(value)
    options = altered['provider_options']
    if change.endswith('_digest'):
        options[change.removesuffix('_digest')]['sha256'] = 'f'*64
    elif change == 'boolean_inode':
        options['node']['inode'] = True
    elif change == 'relative_entry':
        options['entry']['path'] = 'relative/index.js'
    elif change == 'extra_option':
        options['unbound'] = True
    elif change == 'missing_package':
        options.pop('package')
    elif change == 'version':
        altered['reservation']['source']['provider_version'] = 'gitnexus@99.0.0'
    elif change == 'path':
        options['path'] = '/unbound/path'
    elif change == 'environment':
        altered['environment_recipe']['frozen']['EXTRA_NATIVE_VALUE'] = 'unbound'
    elif change == 'policy':
        altered['policy']['io_weight'] = '99'
    elif change == 'mode':
        altered['reservation']['run_mode'] = 'fast'
    with pytest.raises(ValueError):
        bound = snapshot.with_runtime(altered)
        modules['queued'].runtime_inputs(bound, modules)
