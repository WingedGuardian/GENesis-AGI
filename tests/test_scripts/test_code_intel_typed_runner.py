"""Captured shell and scratch schema5; synthetic owner never launches a workload."""
import json
import os
import subprocess
from contextlib import closing

import pytest

from tests.test_scripts.code_intel_consumer_fixture import remove_readiness_fence
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def prepared(tmp_path, monkeypatch, scenario):
    root, source, first = case(tmp_path, monkeypatch, 'runner')
    marker = tmp_path/'owner-observation'
    owner = root/'scripts/lib/code_intel_owner_admission.py'
    owner.write_text(owner.read_text()+'''
def main(snapshot, modules, raw_context, run_mode, settings_path):
    queue = modules['queue']
    context = queue._native.context(json.loads(raw_context), queue._legs, queue.marker_hash)
    assert context['protocol'] == 3
    assert queue.execution_status(context)['launch_reservation'] is None
    held = os.fstat(8)
    Path('''+repr(str(marker))+''').write_text(json.dumps(dict(context=context, mode=run_mode,
        device=held.st_dev, inode=held.st_ino)))
    # No reservation exists: this is genuine structural no-launch restoration.
    queue.reconcile_native()
    fields = ['1','native-outcome',context['hash'],context['provider'],raw_context,'pending']
    scenario = '''+repr(scenario)+'''
    if scenario == 'foreign-context': fields[4] += ' '
    if scenario == 'foreign-hash': fields[2] = '0'*16
    if scenario == 'foreign-provider': fields[3] = 'cbm'
    if scenario == 'bad-state': fields[5] = 'consume'
    if scenario == 'empty': return 0
    raw = b''.join(field.encode()+b'\\0' for field in fields)
    if scenario == 'partial': raw = raw[:-1]
    if scenario == 'extra': raw += b'extra\\0'
    os.write(1,raw)
    return 75 if scenario == 'transport-failure' else 0
''')
    entry = root/'scripts/lib/code_intel_index.sh'
    entry.write_text('#!/bin/bash\nexit 99 # scalar entry must never be called\n')
    remove_readiness_fence(root)
    subprocess.run(['git','-C',str(root),'-c','core.hooksPath=/dev/null','-c','user.name=Fixture',
        '-c','user.email=fixture@example.invalid','commit','-am','synthetic typed owner fixture'],
        check=True,capture_output=True)
    snapshot = source.capture(root,'runner',first.buffers[source.SELF])
    queue = snapshot.assemble()['queue']
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db,queue._legs,queue._native)
    queue.write_marker(str(root),'both','fast')
    return snapshot, queue, marker


@pytest.mark.parametrize('scenario', ['valid','foreign-context','foreign-hash','foreign-provider',
    'bad-state','empty','partial','extra','transport-failure'])
def test_runner_uses_typed_owner_and_exact_outcome_never_scalar_status(tmp_path, monkeypatch, scenario):
    snapshot, queue, marker = prepared(tmp_path,monkeypatch,scenario)
    for name in snapshot.buffers:
        (snapshot.root/name).unlink()
    env = dict(os.environ,CODE_INTEL_RUNNER_IDLE_LOAD='999999',CODE_INTEL_RUNNER_IDLE_IOWAIT='999999',
        CODE_INTEL_RUNNER_IDLE_CLAUDE_CPU='999999')
    with snapshot.sealed() as fd:
        result = subprocess.run(snapshot.resume_command('shell','runner'),stdin=fd,env=env,
                                capture_output=True,text=True,cwd='/',timeout=30)
    assert marker.exists(), result.stdout+result.stderr
    observed = json.loads(marker.read_text())
    assert observed['context']['provider'] == 'gitnexus' and observed['mode'] == 'fast'
    lock = (tmp_path/'state/locks/code-intel-runner.lock').stat()
    assert (observed['device'],observed['inode']) == (lock.st_dev,lock.st_ino)
    assert result.returncode == (0 if scenario == 'valid' else 76), result.stdout+result.stderr
    pending = queue.list_markers()
    assert len(pending) == 2 and {row['provider'] for row in pending} == {'cbm','gitnexus'}
    assert all(row['attempts'] == 0 for row in pending), 'transport status cannot consume or charge a native attempt'
