"""Acceptance reobserves metadata under the original lease without native work."""

import json
import runpy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_owner_admission import runtime_case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


@pytest.mark.parametrize('change', ['policy', 'config', 'sentinel', 'source'])
def test_cbm_observer_refreshes_inputs_inside_one_existing_lease(queue, tmp_path, monkeypatch, change):
    namespace, snapshot, modules, claimed, settings, selected, streams = runtime_case(queue, tmp_path, monkeypatch)
    leases = []
    @contextmanager
    def lease(*, shared):
        assert shared and not leases
        leases.append(True)
        try:
            yield
        finally:
            leases.pop()
    def forbidden(*args, **kwargs):
        raise AssertionError('metadata observation cannot enter queue or launch a provider')
    monkeypatch.setattr(modules['managed'], 'lifecycle_lock', lease)
    def show(unit, *properties, **kwargs):
        assert unit == modules['managed'].BACKEND and properties == ('UnitFileState',)
        return {'UnitFileState': 'enabled'}
    monkeypatch.setattr(modules['managed'], 'show', show)
    monkeypatch.setattr(modules['queue'], '_connect', forbidden)
    monkeypatch.setattr(modules['queue'], 'reserve_launch', forbidden)
    current = dict(selected)
    with namespace['prepared_inputs'](snapshot, modules, claimed['repo_path'], 'cbm', 'fast',
            str(settings), selected, unit=modules['native'].expected_unit(claimed['context']),
            with_observer=True, observe_policy=lambda: current) as inputs:
        observe = inputs['observe']
        before = observe()
        assert leases == [True] and all(stream.closed for stream in streams)
        if change == 'policy':
            current['cpu_quota'] = '100%'
            after = observe()
            assert after['policy_sha256'] != before['policy_sha256']
            assert after['source'] != before['source']
        elif change == 'config':
            settings.write_text(settings.read_text() + '\n')
            assert observe()['source'] != before['source']
        elif change == 'sentinel':
            Path(json.loads(settings.read_text())['sentinel']).touch()
            with pytest.raises((ValueError, RuntimeError)):
                observe()
        else:
            (snapshot.root / 'scripts/lib/code_intel_owner_admission.py').write_text('# changed\n')
            with pytest.raises(ValueError, match='source changed'):
                observe()
        assert leases == [True]
    assert not leases
    with pytest.raises(RuntimeError, match='lease is closed'):
        observe()


def test_gn_observer_notices_changed_binary_without_executing_it(selection, monkeypatch):
    snapshot, modules, environment, cwd, activity, _, node = selection
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_owner_admission.py'))
    selected = namespace['policy'](modules, 'gitnexus', 'full', SimpleNamespace(), {})
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(modules['unit'], 'systemctl_env', lambda: dict(environment))
    unit = modules['native'].expected_unit(dict(hash=modules['queue'].marker_hash(str(snapshot.root)),
                                               provider='gitnexus', claim_id='b' * 32))
    with namespace['prepared_inputs'](snapshot, modules, str(snapshot.root), 'gitnexus', 'full',
            '/unused', selected, unit=unit, recovery=True, with_observer=True,
            observe_policy=lambda: selected) as inputs:
        before = inputs['observe']()
        node.write_text(node.read_text() + '# new bytes\n')
        assert inputs['observe']()['source']['provider_digest'] != before['source']['provider_digest']
    assert not activity.exists()
