"""A prior refusal cannot acknowledge a different native publication."""
import subprocess

import pytest

from tests.test_hooks.test_native_privacy_repeat import markers
from tests.test_hooks.test_native_push_privacy import PUBLIC
from tests.test_hooks.test_native_push_privacy import native_world as native_world


@pytest.mark.parametrize('change', [
    'local-ref', 'remote-ref', 'remote-tip', 'deletion', 'second-ref', 'destination', 'scanner',
])
def test_full_native_identity_preserves_old_permission(native_world, change):
    repo, env, git, commit, invoke = native_world
    base = commit('owned base')
    prior = commit('owned prior', parent=base)
    tip = commit('owned generic', text='10.20.30.40\n', parent=prior)
    assert invoke(tip, base).returncode == 2
    before = markers(repo)
    assert len(before) == 1
    local_ref = 'refs/heads/other' if change == 'local-ref' else 'refs/heads/topic'
    remote_ref = 'refs/heads/other' if change == 'remote-ref' else 'refs/heads/topic'
    data = f'{local_ref} {tip} {remote_ref} {prior if change == "remote-tip" else base}\n'
    if change == 'deletion':
        data += f'(delete) {"0" * len(base)} refs/heads/retired {base}\n'
    elif change == 'second-ref':
        data += f'refs/heads/second {tip} refs/heads/second {base}\n'
    elif change == 'scanner':
        # Only an owned source copy changes; the real branch is untouched.
        with (repo / 'scripts/hooks/push_privacy_scan.py').open('a') as stream:
            stream.write('\n# owned scanner policy revision\n')
        result = subprocess.run(['bash', str(repo / 'scripts/hooks/sync-hooks.sh')],
                                cwd=repo, env=env, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
    url = 'https://github.com/fixture/public.git' if change == 'destination' else PUBLIC
    result = subprocess.run([str(repo / '.git/hooks/pre-push'), 'public', url],
                            cwd=repo, env=env, input=data.encode(), capture_output=True, timeout=70)
    assert result.returncode == 2, result.stderr
    after = markers(repo)
    assert len(after) == 2
    assert all(after[name] == value for name, value in before.items())
    assert b'10.20.30.40' not in result.stdout + result.stderr
