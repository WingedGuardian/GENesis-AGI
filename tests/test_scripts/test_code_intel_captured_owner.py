"""Real sealed owner -> reporter -> harmless marker, with synthetic manager/kernel.

No systemd scope, installed provider or native indexing can be reached even if a
guard is removed. The real canonical locks, child waits, pipes and SQL are used.
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_captured_reporter import prepared
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


@pytest.mark.parametrize('scenario', ['unregistered', 'owner-dies', 'native', 'native-failure'])
def test_actual_captured_initial_owner_closes_only_after_actual_children(tmp_path, monkeypatch, scenario):
    owner_dies, native = scenario == 'owner-dies', scenario in ('native', 'native-failure')
    native_failure = scenario == 'native-failure'
    source, original, modules, _, _, marker = prepared(tmp_path, monkeypatch)
    root = original.root
    proof = root / 'scripts/lib/code_intel_execution.py'
    proof.write_text(proof.read_text() + '''
class ScopeProbe:
 def __init__(self,*args,**kwargs): self.proc=Path('/proc')
 def capture(self,*,pid=None):
  import copy
  pid=os.getpid() if pid is None else pid
  facts=copy.deepcopy(_FIXTURE_FACTS)
  state=_process_state(Path('/proc')/str(pid),pid)
  facts['reporter']=dict(pid=pid,start_ticks=state[1],boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
  return facts
 def finality(self,facts):
  return dict(threads=[os.getpid()],processes=[os.getpid()],pids_current=1)
''')
    scope = root / 'scripts/lib/code_intel_scope.py'
    scope.write_text(scope.read_text() + '''
def _fixture_manager(self,*args,deadline,**kwargs): self.deadline=deadline
ScopeManager.__init__=_fixture_manager
def cleanup_observation(registration,digest,proof,*,owned_wait):
 assert owned_wait is True
 facts=registration['facts']
 return dict(state='removed',registration_sha256=digest,root=facts['root'],mounts_sha256=facts['mounts_sha256'],
             boot_id=facts['reporter']['boot_id'],absent=True,observed_ns=time.monotonic_ns())
''')
    program = Path(sys.executable).resolve()
    build = hashlib.sha256(program.read_bytes()).hexdigest()
    payload = tmp_path / 'native-payload'
    if native:
        admission = root / 'scripts/lib/code_intel_cbm_admission.py'
        admission.write_text(admission.read_text() + '\ndef assess(*args,**kwargs): pass\n')
        worker = root / 'scripts/lib/code_intel_cbm_worker.py'
        worker.write_text(worker.read_text() + '\nBUILD = ' + repr(build)
                          + '\n_PROGRAM = Path(' + repr(str(program)) + ')'
                          + '\n_ENTRY = Path(' + repr(str(marker)) + ')'
                          + '\n_RC = ' + repr(3 if native_failure else 0)
                          + '\n_OUTPUT = ' + repr(str(payload)) + '''
def main(argv,*,queued_factory,queued_native_environment):
 from types import SimpleNamespace
 _ENTRY.write_text('entered')
 args=SimpleNamespace(repo_path=argv[argv.index('--repo-path')+1],
  mode=argv[argv.index('--mode')+1],persistence=argv[argv.index('--persistence')+1])
 settings=Path(argv[argv.index('--managed-config')+1]).read_bytes()
 run=queued_factory(json.loads(settings),args,8*1024**3,settings,queued_native_environment.frozen)
 run.register()
 with _PROGRAM.open('rb') as stream:
  fd=stream.fileno()
  report=run.native([f'/proc/self/fd/{fd}','-I','-S','-c',
   "import pathlib,sys;pathlib.Path(sys.argv[1]).open('a').write('once'+chr(10));sys.exit(int(sys.argv[2]))",_OUTPUT,str(_RC)],
   stream,after_spawn=lambda:None,pass_fds=(fd,),
   env=queued_native_environment.resolve('1'*32,os.environ))
 ok=report['outcome']=='complete' and report['payload_returncode']==0
 run.terminal(provider_ok=ok)
 return 0 if ok else 125
''')
    else:
        managed = root / 'scripts/codebase_managed.py'
        managed.write_text(managed.read_text() + '''
def verified_binary(path,**kwargs): return open('/dev/null','rb')
''')
    owner = root / 'scripts/lib/code_intel_owner.py'
    owner.write_text(owner.read_text() + '''
def scope_command(snapshot,modules,ack_fd):
 return ['/bin/bash','--noprofile','--norc','-p','-c','export INVOCATION_ID='+'1'*32+'; exec "$@"','--',
         *snapshot.resume_command('reporter',str(ack_fd))]
def _fixture_no_manager_action(self,*args):
 raise ValueError('fixture never performs real manager control')
PhysicalOwner._systemctl=_fixture_no_manager_action
''')
    paused = root / 'owner-paused.json'
    if owner_dies:
        owner.write_text(owner.read_text() + '''
_fixture_monitor_command=monitor_command
def monitor_command(*args):
 command=_fixture_monitor_command(*args)
 command[5]=command[5].replace('_wd_send ready\\n','_wd_send ready\\nprintf ready > '+shlex.quote(str(args[0].root/'monitor-ready-before-death'))+'\\n',1)
 return command
def _fixture_pause_before_ack(self):
 while not Path(self.context['repo_path'],'monitor-ready-before-death').exists(): time.sleep(.01)
 kids=[int(pid) for pid in Path('/proc/self/task/'+str(os.getpid())+'/children').read_text().split()]
 Path(self.context['repo_path'],'owner-paused.json').write_text(json.dumps(
  [dict(pid=pid,pgid=os.getpgid(pid),start_ticks=self.proof._process_state(Path('/proc')/str(pid),pid)[1]) for pid in kids]))
 while True: time.sleep(.01)
PhysicalOwner.early=_fixture_pause_before_ack
''')
    config = dict(version=2, main=str(root), binary=str(program if native else root / 'synthetic-binary'),
        cache=str(root / 'cache'), runtime=str(root / 'runtime'), sentinel=str(root / 'disabled'),
        build=build if native else modules['worker'].BUILD)
    settings = root / 'settings.json'
    settings.write_text(json.dumps(config))
    git_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c',
        'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'add', '.'], check=True, capture_output=True, env=git_env)
    subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null', '-c',
        'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'harmless full owner fixture'],
        check=True, capture_output=True, env=git_env)
    snapshot = source.capture(root, 'runner', original.buffers[source.SELF])
    queue = modules['queue']
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
    queue.write_marker(str(root), 'cbm', 'fast')
    monkeypatch.setattr(queue._legs.secrets, 'token_hex', lambda size: '1' * (size * 2))
    claimed = queue.claim_native(queue.marker_hash(str(root)), provider='cbm')
    context = json.dumps(claimed['context'], ensure_ascii=True, separators=(',', ':'))
    # Use the same namespace selected by GENESIS_HOME, not a derived fallback.
    lock = tmp_path / 'state/locks/code-intel-runner.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    command = ['/bin/bash', '--noprofile', '--norc', '-p', '-c',
        'exec 8>"$1"; /usr/bin/flock -n 8 || exit 75; shift; exec "$@"', '--', str(lock),
        *snapshot.resume_command('owner', context, 'fast', str(settings))]
    env = {**os.environ, 'CODE_INTEL_WORKLOAD_SLICE': '1', 'CODE_INTEL_INDEX_DISABLE': '0',
        'CODE_INTEL_CBM_MEMORY_MAX': '8G', 'CODE_INTEL_FAKE_LOADAVG': '0', 'CODE_INTEL_FAKE_IOWAIT': '0',
        'CODE_INTEL_WATCHDOG_INTERVAL': '.02', 'CODE_INTEL_WATCHDOG_WARMUP_INTERVAL': '.02',
        'SYNTHETIC_RUNTIME_VALUE': 'capsule-fixture-private-value'}
    with snapshot.sealed() as capsule:
        child = subprocess.Popen(command, stdin=capsule, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if owner_dies:
        deadline = time.monotonic() + 5
        while not paused.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert paused.exists(), 'owner must reserve and create both children before death'
        children = json.loads(paused.read_text())
        monitor_pid, = [row['pid'] for row in children if row['pid'] == row['pgid']]
        os.kill(child.pid, signal.SIGKILL)  # This fixture's captured owner only.
        try:
            output, errors = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A failed EOF mutant must not leave its disposable monitor behind.
            try:
                birth = next(row['start_ticks'] for row in children if row['pid'] == monitor_pid)
                if modules['proof']._process_state(Path('/proc') / str(monitor_pid), monitor_pid)[1] == birth:
                    os.killpg(monitor_pid, signal.SIGKILL)
            except (ProcessLookupError, FileNotFoundError):
                pass
            child.communicate(timeout=5)
            raise
        assert child.returncode == -signal.SIGKILL and not marker.exists()
        assert output == b''
        state = queue.execution_status(claimed['context'])
        assert state['launch_reservation']['owner']['pid'] == child.pid
        assert state['native_intent'] is state['control_closure'] is None
        assert state['attempts'] == 0 and state['typed_charged_attempts'] is None
        deadline = time.monotonic() + 5
        while Path('/proc', str(monitor_pid)).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        with pytest.raises(ProcessLookupError):
            os.killpg(monitor_pid, 0)
        return
    try:
        output, errors = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()  # This fixture's captured owner only; EOF retires its children.
            child.communicate(timeout=5)
    assert child.returncode == 0, errors.decode()
    assert marker.read_text() == 'entered'
    fields = output.split(b'\0')
    assert fields == [b'1', b'native-outcome', claimed['hash'].encode(), b'cbm', context.encode(),
                      b'consumed' if native and not native_failure else b'pending', b''], errors.decode()
    state = queue.execution_status(claimed['context'])
    assert state['launch_reservation']['owner']['pid'] == child.pid
    closure = state['control_closure']['data']
    assert closure['monitor']['returncode'] == 0
    assert closure['foreground']['returncode'] == (125 if native_failure else 0)
    if native:
        assert payload.read_text() == 'once\n'
        assert state['native_intent']['executable']['sha256'] == build
        assert state['launch_reservation']['source']['provider_digest'] == build
        assert state['scope_registration']['facts'] == closure['startup']['scope']['facts']
        assert state['terminal']['data']['report']['payload_returncode'] == (3 if native_failure else 0)
        assert state['terminal']['data']['report']['payload_started'] is True
        assert state['terminal']['data']['provider_ok'] is (not native_failure)
        assert closure['state'] == 'clean' and closure['reason'] == 'normal'
        assert state['typed_applied_state'] == ('pending' if native_failure else 'consumed')
        assert state['safety_kind'] is None and state['cleanup_proof'] is not None
        assert state['typed_charged_attempts'] == int(native_failure)
        assert state['launch_permission'] == 'spent'
        if native_failure:
            assert queue.list_markers()[0]['attempts'] == 1
        else:
            assert queue.list_markers() == []
        for role in ('foreground', 'monitor'):
            pid = closure[role]['identity']['pid']
            assert not Path('/proc', str(pid)).exists(), 'the actual owner must reap its original children'
        with pytest.raises(ProcessLookupError):
            os.killpg(closure['monitor']['identity']['pid'], 0)
    else:
        assert closure['state'] == 'uncertain' and closure['reason'] == 'startup_incomplete'
        assert state['native_intent'] is None and state['safety_kind'] == 'uncertainty'
        assert queue.list_markers()[0]['attempts'] == 0
    assert b'capsule-fixture-private-value' not in output + errors
