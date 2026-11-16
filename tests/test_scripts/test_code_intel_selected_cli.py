"""Sealed selected CLI keeps both locks, the actual owner and no-resend semantics."""

import importlib.util
import json
import os
import subprocess
import sys
from contextlib import closing

import pytest

from tests.test_scripts.test_code_intel_gitnexus_metadata import selection as selection
from tests.test_scripts.test_code_intel_grant_cli import cli_case, invoke, request
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def selected_fixture(root, observed, *, lost_ack):
    owner = root/'scripts/lib/code_intel_owner.py'
    owner.write_text(owner.read_text()+f'''
import json
class PhysicalOwner:
 def __init__(self, modules, context, reserved, probe, manager):
  self.context,self.reserved=context,reserved
 def run(self, bound):
  assert self.reserved['owner']['pid']==os.getpid()
  assert bound.runtime_value['reservation']==self.reserved
  directory=Path(os.environ['GENESIS_HOME'])/'locks'
  for fd,name in ((8,'code-intel-runner.lock'),(9,'code-intel-'+self.context['hash']+'.lock')):
   held,named=os.fstat(fd),(directory/name).stat()
   assert (held.st_dev,held.st_ino)==(named.st_dev,named.st_ino)
  Path({str(observed)!r}).write_text(json.dumps(dict(pid=os.getpid(),trial=self.context)))
  return None
''')
    if lost_ack:
        facade = root/'scripts/lib/index_marker.py'
        facade.write_text(facade.read_text()+'''
_before_lost_ack = _decision_gate
@contextmanager
def _decision_gate(**kwargs):
 with _before_lost_ack(**kwargs) as admitted:
  db,_=admitted
  before=db.execute("SELECT count(*) FROM executions").fetchone()[0]
  yield admitted
  after=db.execute("SELECT count(*) FROM executions").fetchone()[0]
 if after>before:
  raise OSError('synthetic lost committed acknowledgement')
''')


@pytest.mark.parametrize('lost_ack', [False, True])
def test_captured_selected_cli_executes_only_its_first_acknowledged_owner(selection, tmp_path, lost_ack):
    observed = tmp_path/'selected-owner'
    case = cli_case(selection, tmp_path,
        prepare=lambda root: selected_fixture(root, observed, lost_ack=lost_ack))
    snapshot, queue, _, _, _, activity, capacity = case
    issued = invoke(case, request(case))
    assert issued.returncode == 0, issued.stderr.decode()
    grant = json.loads(issued.stdout)
    assert capacity.read_text() == 'observed'
    args = ['claim-selected', '--repo', str(snapshot.root), '--provider', 'gitnexus',
            '--nonce', grant['nonce'], '--managed-config', '/unused']
    first = invoke(case, args)
    if lost_ack:
        assert first.returncode != 0 and b'lost committed acknowledgement' in first.stderr
        assert not observed.exists()
    else:
        assert first.returncode == 0, first.stderr.decode()
        reply = json.loads(first.stdout)
        assert reply['state'] == 'open' and reply['launch_authority'] is False
        assert json.loads(observed.read_text())['trial'] == reply['trial']
    saved = observed.read_bytes() if observed.exists() else None
    duplicate = invoke(case, args)
    assert duplicate.returncode == 0, duplicate.stderr.decode()
    reply = json.loads(duplicate.stdout)
    assert reply['state'] == 'already_claimed' and reply['launch_authority'] is False
    assert (observed.read_bytes() if observed.exists() else None) == saved
    with closing(queue._connect()) as db:
        grant_now = queue._retry.load(db, grant['hash'], 'gitnexus', queue._native)
        assert grant_now['status'] == 'spent' and grant_now['trial'] == reply['trial']
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM inflight').fetchone()[0] == 1
        row = queue._native.execution(db, reply['trial'], queue._legs)
        assert row['attempts'] == 0 and row['launch_permission'] == 'reserved'
        assert row['native_intent'] is None and row['control_closure'] is None
        assert db.execute("SELECT value FROM queue_meta WHERE key='automatic_protocol_ready'").fetchone()[0] == '0'
    assert queue.list_markers() == [] and not activity.exists()


def test_prior_source_grant_requires_explicit_current_source_reissue(selection, tmp_path, monkeypatch):
    observed = tmp_path/'selected-owner'
    case = cli_case(selection, tmp_path,
        prepare=lambda root: selected_fixture(root, observed, lost_ack=False))
    snapshot, queue, _, _, _, activity, _ = case
    issued = invoke(case, request(case))
    assert issued.returncode == 0, issued.stderr.decode()
    old = json.loads(issued.stdout)
    source = sys.modules[type(snapshot).__module__]
    entry = snapshot.root/source.SELF
    entry.write_bytes(entry.read_bytes()+b'\n# scratch captured generation change\n')
    git_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    subprocess.run(['git', '-C', str(snapshot.root), 'add', source.SELF],
                   env=git_env, check=True, capture_output=True)
    subprocess.run(['git', '-C', str(snapshot.root), '-c', 'core.hooksPath=/dev/null',
        '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'advance captured fixture generation'],
        env=git_env, check=True, capture_output=True)
    raw = entry.read_bytes()
    spec = importlib.util.spec_from_file_location('_updated_source_fixture', entry)
    updated_source = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, updated_source)
    updated_source.__dict__['_ENTRY_BYTES'] = raw
    exec(compile(raw, str(entry), 'exec', dont_inherit=True), updated_source.__dict__)  # noqa: S102 - trusted fixture bytes
    updated = updated_source.capture(snapshot.root, 'runner', raw)
    assert updated.commit != snapshot.commit and updated.files[source.SELF] != snapshot.files[source.SELF]
    args = ['claim-selected', '--repo', str(snapshot.root), '--provider', 'gitnexus',
            '--nonce', old['nonce'], '--managed-config', '/unused']
    assert invoke(case, args).returncode != 0  # An old capsule also remains stale.
    current = (updated, *case[1:])
    refused = invoke(current, args)
    assert refused.returncode != 0 and b'granted inputs' in refused.stderr
    with closing(queue._connect()) as db:
        assert queue._retry.load(db, old['hash'], 'gitnexus', queue._native) == old
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM inflight').fetchone()[0] == 0
    assert not observed.exists() and not activity.exists()
    replacement = invoke(current, request(current))
    assert replacement.returncode == 0, replacement.stderr.decode()
    grant = json.loads(replacement.stdout)
    assert grant['nonce'] != old['nonce'] and grant['status'] == 'issued'
    assert grant['source']['commit'] == updated.commit and grant['source']['files'] == updated.files
    with closing(queue._connect()) as db:
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 0
    selected = invoke(current, [*args[:args.index('--nonce')+1], grant['nonce'], '--managed-config', '/unused'])
    assert selected.returncode == 0, selected.stderr.decode()
    reply = json.loads(selected.stdout)
    assert reply['state'] == 'open' and reply['launch_authority'] is False
    assert json.loads(observed.read_text())['trial'] == reply['trial']
    with closing(queue._connect()) as db:
        assert db.execute('SELECT count(*) FROM executions').fetchone()[0] == 1
        assert db.execute('SELECT attempts FROM executions').fetchone()[0] == 0
    assert not activity.exists()
