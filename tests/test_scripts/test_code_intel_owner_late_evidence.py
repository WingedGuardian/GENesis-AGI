"""Late owner frames affect safety after real child/monitor retirement.

Manager/kernel facts and completed native records are explicit fixtures. The
owned children, ACK/output pipe, private monitor retirement and journal are real.
This isolates control classification; actual bound-factory E2E is separate.
"""
import copy
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owner_loop import prepared
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue
from tests.test_scripts.test_index_marker_execution import _intent
from tests.test_scripts.test_index_marker_native_replay import _terminal


@pytest.mark.parametrize('hierarchical,local', [(0, 0), (1, 0), (0, 1), (1, 1)])
@pytest.mark.parametrize('phase', ['preintent', 'completed'])
def test_late_owned_kill_retains_actual_sample_and_provider_hold(
        queue, tmp_path, evidence, monkeypatch, hierarchical, local, phase):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    sent, deferred, actions, children = [], [], [], []
    release = tmp_path / 'release-frame'
    original_send = owner.modules['queued'].StartupGate.send
    select_module = namespace['select']
    original_select, original_subprocess = select_module.select, namespace['subprocess']

    class ObservedPopen(original_subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

    monkeypatch.setitem(namespace, 'subprocess', SimpleNamespace(
        **{**vars(original_subprocess), 'Popen': ObservedPopen}))

    def send(gate, fd, ack):
        original_send(gate, fd, ack)
        assert queue.register_scope(claimed['context'], ack['scope']) == 'registered'
        if phase == 'completed':
            intent = {**_intent(queue, claimed), 'registration_sha256': queue._native.digest(ack['scope']),
                      'facts': copy.deepcopy(ack['scope']['facts']), 'before_ns': time.monotonic_ns()}
            assert queue.begin_native(claimed['context'], intent) == 'started'
            terminal = _terminal(claimed)
            terminal['sealed_ns'] = time.monotonic_ns()
            for name in ('before', 'after'):
                terminal['report'][name] = copy.deepcopy(ack['scope']['facts'])
            pid = ack['foreground']['pid']
            terminal['report']['finality'] = dict(threads=[pid], processes=[pid], pids_current=1)
            assert queue.remember_native_event(claimed['context'], 'terminal', terminal) == queue.database_path()
            assert queue.execution_status(claimed['context'])['terminal']['data'] == terminal
        sent.append(copy.deepcopy(ack))
        release.write_text('release')
        deadline = time.monotonic() + 5
        # Retain the parent's actual wait authority; never reap in this seam.
        while time.monotonic() < deadline:
            stat = Path('/proc') / str(owner.foreground.identity['pid']) / 'stat'
            if stat.read_text().rpartition(')')[2].split()[0] == 'Z':
                break
            time.sleep(.005)
        else:
            pytest.fail('fixture child did not finish after its single observation')

    def scheduled_select(read, write, exceptional, timeout):
        if sent and not deferred:
            deferred.append(True)
            return [], [], []
        return original_select(read, write, exceptional, timeout)

    def control(action, **kwargs):
        actions.append((action, kwargs))
        pytest.fail('retired monitoring cannot dispatch a late physical action')

    program = '''import copy,hashlib,json,os,pathlib,sys,time
ack=json.loads(os.fdopen(int(sys.argv[1])).read())
assert ack['owner']['pid']==os.getppid()
release=pathlib.Path(sys.argv[5])
while not release.exists(): time.sleep(.001)
facts=copy.deepcopy(ack['scope']['facts'])
facts['events']['oom_kill']=int(sys.argv[3])
facts['local_events']['oom_kill']=int(sys.argv[4])
encode=lambda v:json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
value=dict(reservation_sha256=ack['reservation_sha256'],scope_sha256=hashlib.sha256(encode(ack['scope'])).hexdigest(),facts=facts,observed_ns=time.monotonic_ns())
os.write(1,encode(value)+b'\\n')
pathlib.Path(sys.argv[2]).write_text('entered')
'''
    monkeypatch.setitem(namespace, 'scope_command', lambda snap, mods, fd:
        [sys.executable, '-I', '-c', program, str(fd), str(marker), str(hierarchical), str(local), str(release)])
    monkeypatch.setattr(owner.modules['queued'].StartupGate, 'send', send)
    monkeypatch.setattr(select_module, 'select', scheduled_select)
    monkeypatch.setattr(owner, 'control', control)
    try:
        result = owner.run(snapshot)
    finally:
        release.write_text('release fixture on every assertion/error path')
        for child in children:
            child.wait(timeout=5)
    assert marker.read_text() == 'entered' and sent and deferred
    assert result is not None, owner.diagnostics
    assert result['foreground']['returncode'] == result['monitor']['returncode'] == 0
    assert not actions and result['stop'] is None
    status = queue.execution_status(claimed['context'])
    assert status['scope_registration'] == sent[0]['scope']
    assert (status['native_intent'] is None) == (phase == 'preintent')
    assert (status['terminal'] is None) == (phase == 'preintent')
    assert status['control_closure']['data'] == result
    startup, actual = result['startup'], result['startup']['facts']
    assert startup['scope']['facts']['events']['oom_kill'] == 0
    assert startup['scope']['facts']['local_events']['oom_kill'] == 0
    if hierarchical or local:
        assert status['safety_kind'] in ('resource', 'uncertainty')
        assert actual['events']['oom_kill'] == hierarchical
        assert actual['local_events']['oom_kill'] == local
    else:
        assert status['safety_kind'] is None and result['state'] == 'clean'
