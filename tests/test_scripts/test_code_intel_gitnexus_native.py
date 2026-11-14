"""Real descriptor child and scratch queue; kernel/manager/admission are synthetic."""
import os
import time
from contextlib import closing
from copy import deepcopy

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_code_intel_gitnexus_metadata import adapter
from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


@pytest.mark.parametrize('preparation_refused', [False, True])
@pytest.mark.parametrize('payload_rc', [0, 1, 3, 4, 5, 75])
def test_real_native_seam_requires_registered_preparation_and_durable_intent(
        selection, scope, monkeypatch, preparation_refused, payload_rc):
    snapshot, modules, environment, cwd, _, _, node = selection
    namespace = adapter()
    run = namespace.get('run')
    assert callable(run), 'captured GN reporter must use the shared durable native seam'
    queue = modules['queue']
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
    queue.write_marker(str(snapshot.root), 'gitnexus', 'fast')
    claimed = queue.claim_native(queue.marker_hash(str(snapshot.root)), provider='gitnexus')
    marker = cwd / 'native-entered'
    environment.update(GN_FIXTURE_DB=str(queue.database_path()), GN_FIXTURE_CLAIM=claimed['claim_id'],
        GN_FIXTURE_MARKER=str(marker), GN_FIXTURE_REPO=str(snapshot.root),
        GN_FIXTURE_REFUSE='1' if preparation_refused else '0', GN_FIXTURE_RC=str(payload_rc))
    node.write_text('''#!/usr/bin/python3
import os,pathlib,sqlite3,sys
with sqlite3.connect('file:'+os.environ['GN_FIXTURE_DB']+'?mode=ro',uri=True) as db:
 row=db.execute('SELECT launch_reservation,scope_registration,native_intent FROM executions WHERE claim_id=?',
                (os.environ['GN_FIXTURE_CLAIM'],)).fetchone()
 assert row and row[0] and row[1], 'preparation before reservation/registration'
 if sys.argv[1:]==['--version']:
  assert row[2] is None
  if os.environ['GN_FIXTURE_REFUSE']=='1': sys.exit(97)
  print('v22.23.2')
 else:
  assert row[2], 'payload before durable native intent'
  assert sys.argv[1:]==[os.environ['GITNEXUS_BIN'],'analyze']
  assert pathlib.Path.cwd()==pathlib.Path(os.environ['GN_FIXTURE_REPO'])
  assert pathlib.Path('/proc/self/oom_score_adj').read_text().strip()=='1000'
  pathlib.Path(os.environ['GN_FIXTURE_MARKER']).write_text('entered')
  sys.exit(int(os.environ['GN_FIXTURE_RC']))
''')
    value = runtime(snapshot, modules)
    value['context'] = claimed['context']
    reserved, policy = value['reservation'], value['policy']
    reserved.update(unit=queue._native.expected_unit(claimed['context']), run_mode='fast',
                    route='capped', slice='app-capped.slice', wall_seconds=3600)
    policy.update(route='capped', slice='app-capped.slice', wall_seconds=3600)
    options, recipe, source = namespace['freeze'](snapshot, modules, claimed['context'], reserved,
                                                 policy, environment, cwd=cwd)
    reserved['source'] = source
    value.update(provider_options=options, environment_recipe=recipe)
    assert queue.reserve_launch(claimed['context'], reserved) == 'reserved'
    _, leaf, proc = scope
    target = leaf.parents[1] / 'app-capped.slice' / (reserved['unit']+'.scope')
    target.parent.mkdir()
    leaf.rename(target)
    membership = '/app-capped.slice/'+reserved['unit']+'.scope'
    for path in (proc/'self/cgroup', proc/'self/task'/str(os.getpid())/'cgroup'):
        path.write_text('0::'+membership+'\n')
    (proc/'sys/kernel/random/boot_id').write_text(reserved['owner']['boot_id'])
    original_probe = modules['proof'].ScopeProbe

    class SyntheticProbe(original_probe):
        def __init__(self, unit, cap, *, route):
            super().__init__(unit, cap, route=route, proc=proc)

    class SyntheticManager:
        def __init__(self, *args, deadline):
            self.deadline = deadline
        def registration(self, unit, facts, **kwargs):
            return dict(unit=unit, facts=facts, invocation_id='1'*32, stop_usec=90_000_000)

    monkeypatch.setattr(modules['proof'], 'ScopeProbe', SyntheticProbe)
    monkeypatch.setattr(modules['scope'], 'ScopeManager', SyntheticManager)
    admissions, observations = [], []
    def admit(cap, reserve, unit, *, provider, cache_reserve):
        assert cap == 8*1024**3 and reserve == cache_reserve == 2*1024**3
        assert unit == reserved['unit'] and provider == 'gitnexus'
        assert queue.execution_status(claimed['context'])['scope_registration'] is not None
        admissions.append(time.monotonic_ns())
    monkeypatch.setattr(modules['admission'], 'assess', admit)
    monkeypatch.setenv('INVOCATION_ID', '1'*32)
    probe = SyntheticProbe(reserved['unit'], reserved['cap_bytes'], route=('capped','app-capped.slice'))
    startup = dict(unit=reserved['unit'], facts=deepcopy(probe.capture()),
                   invocation_id='1'*32, stop_usec=90_000_000)
    bound = snapshot.with_runtime(value)
    _, accepted_environment = modules['queued'].runtime_inputs(bound, modules)
    result = run(bound, modules, value, accepted_environment, startup=startup,
                 observe=observations.append, verify=lambda: None)
    state = queue.execution_status(claimed['context'])
    assert observations and admissions
    assert state['control_closure'] is None, 'reporter cannot publish owner closure'
    if preparation_refused:
        assert result != 0 and not marker.exists() and state['native_intent'] is None
        assert state['terminal']['data']['report']['outcome'] == 'refused'
        assert state['terminal']['data']['provider_ok'] is False
    else:
        assert result == (0 if payload_rc == 0 else 111) and marker.read_text() == 'entered'
        assert state['native_intent'] is not None and state['terminal']['data']['provider_ok'] is (payload_rc == 0)
        assert state['terminal']['data']['report']['payload_returncode'] == payload_rc
        assert len(admissions) == 2
