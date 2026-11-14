"""Real sealed reporter exec and ACK; manager/kernel/provider are harmless fixtures."""
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_execution import _facts


def prepared(tmp_path, monkeypatch, *, stock_error=False):
    root, source, snapshot = case(tmp_path, monkeypatch, 'runner')
    # Build the envelope before replacing harmless fixture implementations.
    modules = snapshot.assemble()
    value = runtime(snapshot, modules)
    template = _facts({'context': value['context'], 'mode': value['reservation']['run_mode']})
    template['reporter']['boot_id'] = value['reservation']['owner']['boot_id']
    proof = root / 'scripts/lib/code_intel_execution.py'
    proof.write_text(proof.read_text() + '\n_FIXTURE_FACTS = ' + repr(template) + '''
class ScopeProbe:
    def __init__(self, *args, **kwargs): pass
    def capture(self):
        import copy
        facts = copy.deepcopy(_FIXTURE_FACTS)
        state = _process_state(Path('/proc/self'), os.getpid())
        facts['reporter'] = dict(pid=state[0], start_ticks=state[1],
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        return facts
''')
    manager = root / 'scripts/lib/code_intel_scope.py'
    manager.write_text(manager.read_text() + '''
class ScopeManager:
    def __init__(self, *args, **kwargs): pass
    def require_no_batch(self, physical_reader):
        assert callable(physical_reader)  # This synthetic manager has no prior scopes.
    def _object(self, *args): return '/org/freedesktop/systemd1/unit/owned'
    def registration(self, unit, facts, **kwargs):
        return dict(unit=unit, facts=facts, invocation_id='1'*32, stop_usec=90000000)
''')
    marker = tmp_path / 'fake-provider-entry'
    worker = root / 'scripts/lib/code_intel_cbm_worker.py'
    worker.write_text(worker.read_text() + '''
def main(*args, **kwargs):
    assert callable(kwargs['queued_factory'])
    assert kwargs['queued_native_environment'].frozen['SYNTHETIC_RUNTIME_VALUE'] == 'capsule-fixture-private-value'
    Path(''' + repr(str(marker)) + ''').write_text('entered')
    print('synthetic provider output')
    return 0
''')
    if stock_error:
        actual = Path(__file__).resolve().parents[2] / 'scripts/lib/code_intel_cbm_worker.py'
        worker.write_text(actual.read_text() + '''
def verify_scope(env):
    raise FileNotFoundError(2, 'synthetic settings read failure', 'capsule-fixture-private-value')
''')
    environment = {key: entry for key, entry in os.environ.items() if not key.startswith('GIT_')}
    subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c',
                    'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-am', 'harmless reporter fixture'], env=environment,
                   check=True, capture_output=True)
    snapshot = source.capture(root, 'runner', snapshot.buffers[source.SELF])
    value['reservation']['source'].update(commit=snapshot.commit, files=dict(snapshot.files))
    return source, snapshot.with_runtime(value), modules, value, template, marker


@pytest.mark.parametrize('outcome', ['valid', 'valid-wrapper', 'empty', 'foreign-owner', 'foreign-invocation',
                                    'aliased-output', 'wrapper-alias', 'capsule-stderr',
                                    'duplicate-capsule', 'wrapper-capsule', 'capsule-stdout', 'stock-worker-error'])
def test_actual_captured_reporter_requires_ack_before_harmless_provider_entry(tmp_path, monkeypatch, outcome):
    source, snapshot, modules, runtime_value, template, marker = prepared(
        tmp_path, monkeypatch, stock_error=outcome == 'stock-worker-error')
    reader, writer = os.pipe()
    child = None
    try:
        command = snapshot.resume_command('reporter', str(reader))
        if outcome in ('valid-wrapper', 'wrapper-alias', 'wrapper-capsule'):
            redirect = {'wrapper-alias': 'exec 2>&1; ', 'wrapper-capsule': 'exec 2<&0; '}.get(outcome, '')
            command = ['/bin/bash', '--noprofile', '--norc', '-p', '-c',
                       redirect + 'exec "$@"', '--', *command]
        with snapshot.sealed() as capsule:
            duplicate = None
            if outcome == 'duplicate-capsule':
                duplicate = os.dup(capsule)
            log = capsule if outcome == 'capsule-stderr' else duplicate if duplicate is not None else (
                subprocess.STDOUT if outcome == 'aliased-output' else subprocess.PIPE)
            try:
                output = capsule if outcome == 'capsule-stdout' else subprocess.PIPE
                child = subprocess.Popen(command, stdin=capsule, stdout=output, stderr=log,
                                         pass_fds=(reader,), env={**os.environ, 'INVOCATION_ID': '1' * 32})
            finally:
                if duplicate is not None:
                    os.close(duplicate)
        os.close(reader)
        reader = None
        # A malformed channel may be refused before ACK. The unreaped child's
        # actual birth remains readable even if it already became a zombie.
        start = int(Path(f'/proc/{child.pid}/stat').read_text().rpartition(')')[2].split()[19])
        facts = deepcopy(template)
        facts['reporter'] = dict(pid=child.pid, start_ticks=start,
                                  boot_id=runtime_value['reservation']['owner']['boot_id'])
        scope = dict(unit=runtime_value['reservation']['unit'], facts=facts,
                     invocation_id='1' * 32, stop_usec=90_000_000)
        ack = dict(version=1, reservation_sha256=modules['native'].digest(runtime_value['reservation']),
                   owner=deepcopy(runtime_value['reservation']['owner']), foreground=facts['reporter'],
                   unit_path='/org/freedesktop/systemd1/unit/owned', scope=scope)
        assert not marker.exists()
        if outcome == 'empty':
            os.close(writer)
        else:
            if outcome == 'foreign-owner':
                ack['owner']['start_ticks'] += 1
            elif outcome == 'foreign-invocation':
                ack['scope']['invocation_id'] = '2' * 32
            # Foreign values must reach the real receiving role, not be
            # intercepted by the parent's own validating sender.
            try:
                os.write(writer, modules['native'].encode(ack) + b'\n')
            except BrokenPipeError:
                assert outcome in ('aliased-output', 'wrapper-alias', 'capsule-stderr',
                                   'duplicate-capsule', 'wrapper-capsule', 'capsule-stdout'), 'unexpected early refusal'
            os.close(writer)
        writer = None
        stdout, stderr = child.communicate(timeout=30)
        stdout = stdout or b''
        stderr = stderr or b''
        if outcome in ('valid', 'valid-wrapper'):
            assert child.returncode == 0, stderr.decode()
            assert marker.read_text() == 'entered'
            observation = json.loads(stdout)
            assert observation['scope_sha256'] == modules['native'].digest(scope)
            assert observation['facts']['reporter'] == facts['reporter']
            assert b'synthetic provider output' not in stdout and b'synthetic provider output' in stderr
        else:
            assert child.returncode != 0 and not marker.exists()
            if outcome == 'capsule-stdout':
                assert b'source capsule aliases a standard output' in stderr
            if outcome == 'stock-worker-error':
                assert child.returncode == 125
                assert b'FileNotFoundError' in stderr
                assert b'stock worker refused/failed' not in stderr
        assert b'capsule-fixture-private-value' not in stdout + stderr
    finally:
        for fd in (reader, writer):
            if fd is not None:
                os.close(fd)
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate()
