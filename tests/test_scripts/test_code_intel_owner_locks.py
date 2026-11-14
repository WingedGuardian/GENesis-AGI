"""Canonical repository flock lives on fd9 only inside the owned child process."""
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHILD = '''import json,os,runpy,sys,types
admission=runpy.run_path(sys.argv[1])
queue=types.SimpleNamespace(**runpy.run_path(sys.argv[2]))
context=json.loads(sys.argv[3])
try:
 with admission['repository_lock']({'queue':queue},context) as fd:
  print(json.dumps({'fd':fd,'inode':os.fstat(fd).st_ino,'inheritable':os.get_inheritable(fd)}),flush=True)
  sys.stdin.buffer.read(1)
except (OSError,ValueError) as error:
 print('LOCK_REFUSED:'+type(error).__name__,file=sys.stderr)
 raise SystemExit(75)
'''


def prepared(tmp_path):
    repo = tmp_path / "repo with 'quotes' 雪"
    (repo / '.git').mkdir(parents=True)
    home = tmp_path / 'state'
    directory = home / 'locks'
    directory.mkdir(parents=True)
    digest = hashlib.sha1(str(repo).encode()).hexdigest()[:16]  # noqa: S324 - canonical compatibility spelling
    context = dict(hash=digest, repo_path=str(repo), provider='cbm', protocol=3,
                   execution_seq=1, claim_id='1' * 32)
    command = [sys.executable, '-I', '-B', '-c', CHILD, str(ROOT / 'scripts/lib/code_intel_owner_admission.py'),
               str(ROOT / 'scripts/lib/index_marker.py'), json.dumps(context)]
    return command, {**os.environ, 'GENESIS_HOME': str(home)}, directory / ('code-intel-' + digest + '.lock')


def test_repository_lock_holds_canonical_inode_on_noninherited_fd9(tmp_path):
    command, env, path = prepared(tmp_path)
    child = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        line = child.stdout.readline()
        assert line, child.stderr.read().decode()
        info = json.loads(line)
        assert info == dict(fd=9, inode=path.stat().st_ino, inheritable=False)
        with path.open('rb') as separate, pytest.raises(BlockingIOError):
            fcntl.flock(separate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output, errors = child.communicate(b'x', timeout=30)
        assert child.returncode == 0 and not output, errors
        with path.open('rb') as separate:
            fcntl.flock(separate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


def test_existing_freeze_flock_is_never_bypassed(tmp_path):
    command, env, path = prepared(tmp_path)
    with path.open('wb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = path.stat().st_ino
        child = subprocess.run(command, env=env, input=b'x', capture_output=True, timeout=30)
        assert child.returncode == 75 and not child.stdout and b'LOCK_REFUSED:BlockingIOError' in child.stderr
        assert path.stat().st_ino == original


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'fifo'])
def test_nonregular_lock_leaf_is_refused_without_fallback(tmp_path, kind):
    command, env, path = prepared(tmp_path)
    if kind == 'symlink':
        target = path.parent / 'retained-target'
        target.write_text('retained')
        path.symlink_to(target)
    elif kind == 'directory':
        path.mkdir()
    else:
        os.mkfifo(path)
    child = subprocess.run(command, env=env, input=b'x', capture_output=True, timeout=30)
    assert child.returncode == 75 and not child.stdout and b'LOCK_REFUSED:' in child.stderr
    if kind == 'symlink':
        assert target.read_text() == 'retained' and path.is_symlink()
