"""The approved 360-cell verdict matrix, through installed owned hook copies.

These invoke the native hook with real local Git objects and complete tuples;
they do not claim 360 remote pushes. Separate installed push/caller controls
qualify transport and publication behavior.
"""
import itertools
import json
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_hooks.test_native_privacy_repeat import markers
from tests.test_hooks.test_native_push_privacy import CANARY, PUBLIC
from tests.test_hooks.test_native_push_privacy import native_world as native_world

AXES = itertools.product(
    ('A', 'B', 'could-not-scan'), ('foreground', 'automated'),
    ('first', 'identical', 'changed', 'expired'), ('public', 'private', 'undeclared'),
    ('branch', 'new-ref', 'deletion-update', 'merge', 'tag'),
)


@pytest.mark.parametrize('tier,caller,attempt,destination,shape', AXES)
def test_installed_privacy_matrix(native_world, record_property, tier, caller, attempt, destination, shape):
    repo, env, git, commit, invoke = native_world
    # All values and configuration belong to this disposable fixture.
    payload = {'A': CANARY, 'B': '10.20.30.40', 'could-not-scan': 'owned safe message'}[tier]
    base = commit('owned safe base')
    oid = commit(payload, parent=base)
    remote_oid = base if shape == 'branch' else '0' * len(base)
    ref = 'refs/heads/topic'
    if shape == 'merge':
        other = commit('owned other parent', parent=base)
        tree = git('rev-parse', base + '^{tree}').decode().strip()
        oid = git('commit-tree', tree, '-p', base, '-p', other, '-m', payload).decode().strip()
    elif shape == 'tag':
        raw = f'object {base}\ntype commit\ntag owned\ntagger Fixture <fixture@example.invalid> 1 +0000\n\n{payload}\n'.encode()
        oid = git('hash-object', '-w', '-t', 'tag', '--stdin', data=raw).decode().strip()
        ref = 'refs/tags/owned'
    data = f'{ref} {oid} {ref} {remote_oid}\n'.encode()
    if shape == 'deletion-update':
        data += f'(delete) {"0" * len(base)} refs/heads/retired {base}\n'.encode()
    if tier == 'could-not-scan':
        Path(env['GENESIS_RELEASE_FINGERPRINTS']).unlink()
    url = PUBLIC
    if destination == 'private':
        url = 'git@github.com:fixture/private.git'
    elif destination == 'undeclared':
        (Path(env['HOME']) / '.genesis/config/genesis.yaml').write_text('{}\n')
        git('remote', 'add', 'origin', PUBLIC)
    installed = subprocess.run(['bash', str(repo / 'scripts/hooks/sync-hooks.sh')],
                               cwd=repo, env=env, capture_output=True, timeout=10)
    assert installed.returncode == 0, installed.stderr

    def run(population):
        return subprocess.run([str(repo / '.git/hooks/pre-push'), 'public', url],
                              cwd=repo, env=env, input=population, capture_output=True, timeout=70)

    env.pop('GENESIS_AUTOMATED_PUSH', None)
    if attempt != 'first':
        seeded = run(data)
        assert seeded.returncode == (0 if destination == 'private' else 2), seeded.stderr
    if attempt == 'changed':
        data = data.replace(ref.encode(), (ref + '-changed').encode())
    elif attempt == 'expired':
        for name in markers(repo):
            path = repo / '.git/genesis/privacy-ack' / name
            value = json.loads(path.read_text())
            value['created_at'] = time.time() - 86401
            path.write_text(json.dumps(value))
    before = markers(repo)
    if caller == 'automated':
        env['GENESIS_AUTOMATED_PUSH'] = '1'
    result = run(data)
    expected = (0 if destination == 'private' else
                2 if tier == 'A' or caller == 'automated' else
                0 if attempt == 'identical' else 2)
    record_property('privacy_matrix', json.dumps({
        'tier': tier, 'caller': caller, 'attempt': attempt, 'destination': destination,
        'ref_shape': shape, 'expected_exit': expected, 'actual_exit': result.returncode,
        'marker_count_before': len(before), 'marker_count_after': len(markers(repo)),
    }, sort_keys=True))
    assert result.returncode == expected, result.stderr
    assert CANARY.encode() not in result.stdout + result.stderr
    assert b'10.20.30.40' not in result.stdout + result.stderr
    after = markers(repo)
    if destination == 'private' or tier == 'A':
        assert after == before == {}
    elif caller == 'automated':
        assert after == before
    elif attempt == 'identical':
        assert len(before) == 1 and after == {}
    elif attempt == 'changed':
        assert len(after) == 2 and all(after[name] == value for name, value in before.items())
    else:
        assert len(after) == 1
