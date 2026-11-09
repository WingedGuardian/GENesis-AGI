"""Real sealed owner/reporter/native child/SQL; synthetic manager and kernel facts.

No installed provider, systemd operation or indexing is reachable by this fixture.
"""
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_runtime_capsule import runtime
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules
from tests.test_scripts.test_index_marker_execution import _facts


@pytest.mark.parametrize('payload_rc', [0, 3, 75])
def test_sealed_initial_gn_owner_runs_preparation_native_and_durable_settlement(selection, monkeypatch, payload_rc):
    original, modules, environment, cwd, marker, _, node = selection
    root = original.root
    source = sys.modules['_source_fixture']
    node.write_text('''#!/usr/bin/python3
import os,pathlib,sqlite3,sys
with sqlite3.connect('file:'+os.environ['GN_FIXTURE_DB']+'?mode=ro',uri=True) as db:
 row=db.execute("SELECT scope_registration,native_intent FROM executions WHERE provider='gitnexus'").fetchone()
 assert row and row[0], 'preparation before durable registration'
 if sys.argv[1:]==['--version']:
  assert row[1] is None
  print('v22.23.2')
 else:
  assert row[1], 'native child before intent'
  assert sys.argv[1:]==[os.environ['GITNEXUS_BIN'],'analyze']
  assert pathlib.Path.cwd()==pathlib.Path(os.environ['GN_FIXTURE_REPO'])
  assert pathlib.Path('/proc/self/oom_score_adj').read_text().strip()=='1000'
  pathlib.Path(os.environ['GN_FIXTURE_MARKER']).write_text('native entered')
  sys.exit(int(os.environ['GN_FIXTURE_RC']))
''')
    value = runtime(original, modules)
    facts = _facts(dict(context=value['context'], mode='fast'))
    proof = root/'scripts/lib/code_intel_execution.py'
    proof.write_text(proof.read_text()+'\n_FIXTURE_FACTS='+repr(facts)+'''
class ScopeProbe:
 def __init__(self,unit,cap,*,route): self.unit,self.route,self.proc=unit,route,Path('/proc')
 def capture(self,*,pid=None):
  import copy
  pid=os.getpid() if pid is None else pid
  facts=copy.deepcopy(_FIXTURE_FACTS)
  state=_process_state(Path('/proc')/str(pid),pid)
  facts['reporter']=dict(pid=pid,start_ticks=state[1],boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
  facts['leaf']['path']=facts['root']['path']+'/'+self.route[1]+'/'+self.unit+'.scope'
  facts['membership']='0::/'+self.route[1]+'/'+self.unit+'.scope\\n'
  return facts
 def finality(self,facts):
  return dict(threads=[os.getpid()],processes=[os.getpid()],pids_current=1)
''')
    scope = root/'scripts/lib/code_intel_scope.py'
    scope.write_text(scope.read_text()+'''
class ScopeManager:
 def __init__(self,*args,deadline): self.deadline=deadline
 def _object(self,*args): return '/org/freedesktop/systemd1/unit/owned'
 def registration(self,unit,facts,**kwargs):
  return dict(unit=unit,facts=facts,invocation_id='1'*32,stop_usec=90000000)
def cleanup_observation(registration,digest,proof,*,owned_wait):
 assert owned_wait is True
 facts=registration['facts']
 return dict(state='removed',registration_sha256=digest,root=facts['root'],mounts_sha256=facts['mounts_sha256'],
             boot_id=facts['reporter']['boot_id'],absent=True,observed_ns=time.monotonic_ns())
''')
    admission = root/'scripts/lib/code_intel_cbm_admission.py'
    admission.write_text(admission.read_text()+'''
def assess(cap,reserve,unit,*,provider,cache_reserve):
 assert cap==8589934592 and reserve==cache_reserve==2147483648 and provider=='gitnexus'
''')
    owner = root/'scripts/lib/code_intel_owner.py'
    owner.write_text(owner.read_text()+'''
def scope_command(snapshot,modules,ack_fd):
 return ['/bin/bash','--noprofile','--norc','-p','-c','export INVOCATION_ID='+'1'*32+'; exec "$@"','--',
         *snapshot.resume_command('reporter',str(ack_fd))]
def _fixture_no_manager_action(self,*args):
 raise ValueError('fixture never performs real manager control')
PhysicalOwner._systemctl=_fixture_no_manager_action
''')
    subprocess.run(['git','-C',str(root),'-c','core.hooksPath=/dev/null','-c','user.name=Fixture',
        '-c','user.email=fixture@example.invalid','commit','-am','harmless GN owner fixture'],check=True,capture_output=True)
    snapshot = source.capture(root,'runner',original.buffers[source.SELF])
    queue = modules['queue']
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db,queue._legs,queue._native)
    queue.write_marker(str(root),'both','fast')
    claimed = queue.claim_native(queue.marker_hash(str(root)),provider='gitnexus')
    context = json.dumps(claimed['context'],ensure_ascii=True,separators=(',',':'))
    lock = Path(environment['GENESIS_HOME'])/'locks/code-intel-runner.lock'
    lock.parent.mkdir(parents=True)
    command = ['/bin/bash','--noprofile','--norc','-p','-c',
        'exec 8>"$1"; /usr/bin/flock -n 8 || exit 75; shift; exec "$@"','--',str(lock),
        *snapshot.resume_command('owner',context,'fast','/unused/settings')]
    env = dict(os.environ,**environment)
    env.update(CODE_INTEL_WORKLOAD_SLICE='1',CODE_INTEL_INDEX_DISABLE='0',CODE_INTEL_GITNEXUS_MEMORY_MAX='8G',
        CODE_INTEL_FAKE_LOADAVG='0',CODE_INTEL_FAKE_IOWAIT='0',CODE_INTEL_WATCHDOG_INTERVAL='.02',
        CODE_INTEL_WATCHDOG_WARMUP_INTERVAL='.02',GN_FIXTURE_DB=str(queue.database_path()),
        GN_FIXTURE_MARKER=str(marker),GN_FIXTURE_REPO=str(root),GN_FIXTURE_RC=str(payload_rc))
    with snapshot.sealed() as fd:
        result = subprocess.run(command,stdin=fd,env=env,cwd=cwd,capture_output=True,timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    expected = 'consumed' if payload_rc == 0 else 'pending'
    assert result.stdout.split(b'\0') == [b'1',b'native-outcome',claimed['hash'].encode(),b'gitnexus',
                                        context.encode(),expected.encode(),b'']
    state = queue.execution_status(claimed['context'])
    assert marker.read_text() == 'native entered'
    assert state['terminal']['data']['report']['payload_returncode'] == payload_rc
    assert state['control_closure']['data']['state'] == 'clean' and state['safety_kind'] is None
    pending = {row['provider']:row for row in queue.list_markers()}
    assert pending['cbm']['attempts'] == 0
    if payload_rc:
        assert pending['gitnexus']['attempts'] == 1
    else:
        assert 'gitnexus' not in pending
