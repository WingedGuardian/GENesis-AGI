"""Sealed reporter to real scratch journal and harmless descriptor-owned child."""
import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_execution import _facts, _schema5
from tests.test_scripts.test_index_marker_legs import queue as queue


@pytest.mark.parametrize('phase,counter', [('valid', 'events')]
                         + [(phase, counter) for phase in ('initial', 'register', 'preintent')
                            for counter in ('events', 'local_events')])
def test_sealed_reporter_bound_factory_refuses_sampled_kills_before_actual_child(
        tmp_path, monkeypatch, queue, phase, counter):
    root, source, snapshot = case(tmp_path, monkeypatch, 'runner')
    _schema5(queue)
    queue.write_marker(str(root), 'cbm', 'full')
    claimed = queue.claim_native(queue.marker_hash(str(root)), provider='cbm')
    template = _facts(claimed)
    phase_file, entry, output = (tmp_path / name for name in ('phase', 'worker-entry', 'native-output'))
    phase_file.write_text('initial')
    executable = Path(sys.executable).resolve()
    proof = root / 'scripts/lib/code_intel_execution.py'
    proof.write_text(proof.read_text() + '\n_TEMPLATE = ' + repr(template)
                     + '\n_PHASE_FILE = Path(' + repr(str(phase_file)) + ')'
                     + '\n_FAULT_PHASE = ' + repr(phase) + '\n_COUNTER = ' + repr(counter) + '''
class ScopeProbe:
    def __init__(self, *args, **kwargs): self.proc = Path('/proc')
    def capture(self):
        import copy
        facts = copy.deepcopy(_TEMPLATE)
        pid, start, parent = _process_state(Path('/proc/self'), os.getpid())
        facts['reporter'] = dict(pid=pid, start_ticks=start,
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        if _PHASE_FILE.read_text() == _FAULT_PHASE:
            facts[_COUNTER]['oom_kill'] = 1
        return facts
    def finality(self, facts):
        return dict(threads=[os.getpid()], processes=[os.getpid()], pids_current=1)
''')
    manager = root / 'scripts/lib/code_intel_scope.py'
    manager.write_text(manager.read_text() + '''
class ScopeManager:
    def __init__(self, *args, **kwargs): pass
    def _object(self, *args): return '/org/freedesktop/systemd1/unit/owned'
    def registration(self, unit, facts, **kwargs):
        return dict(unit=unit, facts=facts, invocation_id='1'*32, stop_usec=90000000)
''')
    admission = root / 'scripts/lib/code_intel_cbm_admission.py'
    admission.write_text(admission.read_text() + '\ndef assess(*args, **kwargs): pass\n')
    worker = root / 'scripts/lib/code_intel_cbm_worker.py'
    worker.write_text(worker.read_text() + '\nBUILD = ' + repr(hashlib.sha256(executable.read_bytes()).hexdigest())
                      + '\n_PHASE_FILE = Path(' + repr(str(phase_file)) + ')'
                      + '\n_ENTRY = Path(' + repr(str(entry)) + ')'
                      + '\n_OUTPUT = ' + repr(str(output))
                      + '\n_PROGRAM = Path(' + repr(str(executable)) + ')' + '''
def main(argv, *, queued_factory, queued_native_environment):
    from types import SimpleNamespace
    _ENTRY.write_text('entered')
    args = SimpleNamespace(repo_path=argv[argv.index('--repo-path')+1], mode='full', persistence='true')
    run = queued_factory({}, args, 8*1024**3, b'{}', queued_native_environment.frozen)
    _PHASE_FILE.write_text('register')
    run.register()
    _PHASE_FILE.write_text('preintent')
    with _PROGRAM.open('rb') as stream:
        fd = stream.fileno()
        report = run.native([f'/proc/self/fd/{fd}', '-I', '-S', '-c',
            "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('once')", _OUTPUT],
            stream, after_spawn=lambda:None, pass_fds=(fd,),
            env=queued_native_environment.resolve('1'*32, os.environ))
    run.terminal(provider_ok=report['outcome']=='complete')
    return 0 if report['outcome']=='complete' else 125
''')
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c',
                    'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-am', 'harmless bound reporter'], env=environment, check=True, capture_output=True)
    snapshot = source.capture(root, 'runner', snapshot.buffers[source.SELF])
    for _, alias, _ in source.MODULES:
        monkeypatch.delitem(sys.modules, alias, raising=False)
    modules = snapshot.assemble()
    value = runtime(snapshot, modules)
    value['context'] = claimed['context']
    reserved = value['reservation']
    reserved['unit'] = modules['native'].expected_unit(claimed['context'])
    reserved['source'] = modules['queued'].cbm_source(snapshot=snapshot, worker=modules['worker'],
        config={}, args=SimpleNamespace(repo_path=str(root), mode='full', persistence='true'),
        cap=reserved['cap_bytes'], settings=b'{}', environment=value['environment_recipe']['frozen'],
        policy=value['policy'], registration_slots=value['environment_recipe']['registration_slots'])
    assert modules['queue'].reserve_launch(claimed['context'], reserved) == 'reserved'
    before = modules['queue'].execution_status(claimed['context'])
    snapshot = snapshot.with_runtime(value)
    reader, writer = os.pipe()
    child = None
    try:
        with snapshot.sealed() as capsule:
            child = subprocess.Popen(snapshot.resume_command('reporter', str(reader)), stdin=capsule,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(reader,),
                env={**os.environ, 'INVOCATION_ID':'1'*32})
        os.close(reader)
        reader = None
        start = int(Path(f'/proc/{child.pid}/stat').read_text().rpartition(')')[2].split()[19])
        facts = deepcopy(template)
        facts['reporter'] = dict(pid=child.pid, start_ticks=start, boot_id=reserved['owner']['boot_id'])
        original = dict(unit=reserved['unit'], facts=facts, invocation_id='1'*32, stop_usec=90000000)
        ack = dict(version=1, reservation_sha256=modules['native'].digest(reserved), owner=reserved['owner'],
                   foreground=facts['reporter'], unit_path='/org/freedesktop/systemd1/unit/owned', scope=original)
        modules['queued'].StartupGate(modules['queue'], modules['proof'], claimed['context'], reserved).send(writer, ack)
        writer = None
        stdout, stderr = child.communicate(timeout=30)
        samples = [json.loads(line)['facts'] for line in stdout.splitlines()]
        status = modules['queue'].execution_status(claimed['context'])
        if phase == 'valid':
            assert child.returncode == 0, stderr.decode()
            assert output.read_text() == 'once'
            assert status['scope_registration'] == original
            assert status['native_intent'] is not None
            assert status['terminal']['data']['provider_ok'] is True
            assert len(samples) >= 4
        else:
            assert child.returncode == 125 and not output.exists(), stderr.decode()
            assert any(facts[counter]['oom_kill'] == 1 for facts in samples)
            assert status['native_intent'] is None
            assert status['typed_charged_attempts'] == before['typed_charged_attempts'] is None
            assert (status['scope_registration'] is not None) is (phase == 'preintent')
            assert entry.exists() is (phase != 'initial')
        assert b'capsule-fixture-private-value' not in stdout + stderr
    finally:
        for fd in (reader, writer):
            if fd is not None:
                os.close(fd)
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate()
