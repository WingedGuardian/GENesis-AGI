"""Initial GN admission binds metadata without preparing either provider."""
import json
import runpy
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def initial(selection, monkeypatch):
    snapshot, modules, environment, cwd, activity, _, _ = selection
    queue = modules['queue']
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
    queue.write_marker(str(snapshot.root), 'gitnexus', 'fast')
    context = queue.claim_native(queue.marker_hash(str(snapshot.root)), provider='gitnexus')['context']
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/lib/code_intel_owner_admission.py'))
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(modules['unit'], 'systemctl_env', lambda: dict(environment))
    def forbidden(*args, **kwargs):
        raise AssertionError('GN reservation must not read managed CBM settings or take its lifecycle lock')
    monkeypatch.setattr(modules['managed'], 'lifecycle_lock', forbidden)
    monkeypatch.setattr(modules['managed'], 'runtime_config', forbidden)
    selected = namespace['policy'](modules, 'gitnexus', 'fast', SimpleNamespace(), {})
    return snapshot, modules, activity, context, namespace, selected


@pytest.mark.parametrize('lost_ack', [False, True])
def test_initial_gn_reservation_is_actual_self_metadata_only_and_never_resends(selection, monkeypatch, lost_ack):
    snapshot, modules, activity, context, namespace, selected = initial(selection, monkeypatch)
    queue = modules['queue']
    actual_owner = queue._recovery.owner_identity()
    original = queue.reserve_launch
    def reserve(context, value):
        assert value['owner'] == queue._recovery.owner_identity() and not activity.exists()
        result = original(context, value)
        if lost_ack:
            raise OSError('synthetic lost reservation acknowledgement')
        return result
    monkeypatch.setattr(queue, 'reserve_launch', reserve)
    if lost_ack:
        with pytest.raises(OSError):
            namespace['reserve_runtime'](snapshot, modules, context, 'fast', '/unused/settings', selected,
                                         actual_owner=actual_owner)
    else:
        bound = namespace['reserve_runtime'](snapshot, modules, context, 'fast', '/unused/settings', selected,
                                             actual_owner=actual_owner)
        value, _ = modules['queued'].runtime_inputs(bound, modules)
        assert value['provider_options']['cwd'] == str(selection[3])
    before = queue.execution_status(context)['launch_reservation']
    assert before is not None and not activity.exists()
    monkeypatch.setattr(queue, 'reserve_launch', original)
    with pytest.raises(ValueError):
        namespace['reserve_runtime'](snapshot, modules, context, 'fast', '/unused/settings', selected,
                                     actual_owner=actual_owner)
    assert queue.execution_status(context)['launch_reservation'] == before and not activity.exists()


@pytest.mark.parametrize('field', ['pid', 'start_ticks', 'boot_id'])
@pytest.mark.parametrize('seam', ['argument', 'before-cas', 'policy'])
def test_gn_original_owner_birth_fences_every_admission_boundary(selection, monkeypatch, field, seam):
    snapshot, modules, activity, context, namespace, selected = initial(selection, monkeypatch)
    queue = modules['queue']
    owner = queue._recovery.owner_identity()
    changed = {**owner, field: (owner[field] + 1 if field != 'boot_id'
                               else owner[field][:-1] + ('0' if owner[field][-1] != '0' else '1'))}
    inputs = []
    freeze = modules['gitnexus'].freeze
    def capture(*args, **kwargs):
        inputs.append('metadata')
        return freeze(*args, **kwargs)
    monkeypatch.setattr(modules['gitnexus'], 'freeze', capture)
    if seam == 'policy':
        def drift(*args, **kwargs):
            monkeypatch.setattr(queue._recovery, 'owner_identity', lambda: changed)
            return selected
        globals_ = namespace['main'].__globals__
        monkeypatch.setitem(globals_, 'repository_lock', lambda *args: nullcontext())
        monkeypatch.setitem(globals_, 'policy', drift)
        monkeypatch.setitem(globals_, 'reserve_runtime', lambda *args, **kwargs:
                            pytest.fail('changed GN owner birth reached reserve_runtime'))
        monkeypatch.setattr(modules['scope'], 'ScopeManager', lambda *args, **kwargs: SimpleNamespace())
        assert namespace['main'](snapshot, modules, json.dumps(context), 'fast', '/unused/settings') == 76
    else:
        original = modules['queued'].runtime_inputs
        def drift(*args, **kwargs):
            value = original(*args, **kwargs)
            monkeypatch.setattr(queue._recovery, 'owner_identity', lambda: changed)
            return value
        if seam == 'before-cas':
            monkeypatch.setattr(modules['queued'], 'runtime_inputs', drift)
        with pytest.raises(ValueError, match='owner'):
            namespace['reserve_runtime'](snapshot, modules, context, 'fast', '/unused/settings', selected,
                actual_owner=changed if seam == 'argument' else owner)
    assert inputs == (['metadata'] if seam == 'before-cas' else [])
    assert not activity.exists() and queue.execution_status(context)['launch_reservation'] is None
