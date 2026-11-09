"""Reporter brackets its own birth, live parent and original manager/kernel facts."""
import os
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_code_intel_queued_session import session as session
from tests.test_scripts.test_index_marker_legs import queue as queue


def state(path, *, parent=None, start=None):
    raw = path.read_text()
    head, _, tail = raw.rpartition(')')
    fields = tail.split()
    if parent is not None:
        fields[1] = str(parent)
    if start is not None:
        fields[19] = str(start)
    path.write_text(head + ') ' + ' '.join(fields))


def prepared(session, queue):
    run, claimed, reserved, program, source, order, leaf, namespace = session()
    scope = run.manager.registration(reserved['unit'], run.probe.capture(), wall_seconds=reserved['wall_seconds'])
    parent = os.getppid()
    parent_raw = Path(f'/proc/{parent}/stat').read_text()
    reserved['owner'] = dict(pid=parent, start_ticks=int(parent_raw.rpartition(')')[2].split()[19]),
                             boot_id=scope['facts']['reporter']['boot_id'])
    (run.probe.proc / str(parent)).mkdir()
    (run.probe.proc / str(parent) / 'stat').write_text(parent_raw)
    state(run.probe.proc / 'self/stat', parent=parent)
    value = dict(version=1, reservation_sha256=queue._native.digest(reserved), owner=reserved['owner'],
                 foreground=deepcopy(scope['facts']['reporter']),
                 unit_path='/org/freedesktop/systemd1/unit/owned', scope=scope)
    run.manager._object = lambda *args: value['unit_path']
    gate = namespace['StartupGate'](queue, run.proof, claimed['context'], reserved)
    operation = getattr(gate, 'verify', None)
    assert callable(operation), 'startup ACK needs fresh reporter/parent and original scope verification'
    return operation, value, run


def verify(operation, value, run, environment=None):
    actual = {'INVOCATION_ID': value['scope']['invocation_id']} if environment is None else environment
    return operation(value, run.probe, run.manager, actual, proc=run.probe.proc)


def test_reporter_can_bind_same_lifetime_with_monotonically_new_facts(session, queue):
    operation, value, run = prepared(session, queue)
    leaf = Path(value['scope']['facts']['leaf']['path'])
    (leaf / 'memory.events').write_text('low 0\nhigh 1\nmax 0\noom 0\noom_kill 0\n')
    result = verify(operation, value, run)
    assert result['facts']['events']['high'] == 1
    assert value['scope']['facts']['events']['high'] == 0


@pytest.mark.parametrize('fault', ['self-birth', 'parent-birth', 'parent', 'boot', 'invocation',
                                  'object-path', 'leaf', 'missing-invocation', 'foreign-invocation'])
def test_foreign_parent_reporter_manager_or_kernel_cannot_release_preparation(session, queue, fault):
    operation, value, run = prepared(session, queue)
    actual = None
    if fault == 'self-birth':
        state(run.probe.proc / 'self/stat', start=1001)
    elif fault == 'parent-birth':
        state(run.probe.proc / str(value['owner']['pid']) / 'stat', start=value['owner']['start_ticks'] + 1)
    elif fault == 'parent':
        state(run.probe.proc / 'self/stat', parent=1)
    elif fault == 'boot':
        (run.probe.proc / 'sys/kernel/random/boot_id').write_text('22222222-2222-4222-8222-222222222222')
    elif fault == 'invocation':
        original = run.manager.registration
        run.manager.registration = lambda *args, **kwargs: {**original(*args, **kwargs), 'invocation_id': '2' * 32}
    elif fault == 'object-path':
        run.manager._object = lambda *args: '/org/freedesktop/systemd1/unit/foreign'
    elif fault == 'leaf':
        original = run.probe.capture
        def foreign():
            result = original()
            result['leaf']['inode'] += 1
            return result
        run.probe.capture = foreign
    elif fault == 'missing-invocation':
        actual = {}
    else:
        actual = {'INVOCATION_ID': '2' * 32}
    with pytest.raises(ValueError):
        verify(operation, value, run, actual)


def test_parent_birth_is_checked_again_after_manager_and_kernel_reads(session, queue):
    operation, value, run = prepared(session, queue)
    original = run.probe.capture
    def change_parent():
        result = original()
        state(run.probe.proc / str(value['owner']['pid']) / 'stat', start=value['owner']['start_ticks'] + 1)
        return result
    run.probe.capture = change_parent
    with pytest.raises(ValueError):
        verify(operation, value, run)
