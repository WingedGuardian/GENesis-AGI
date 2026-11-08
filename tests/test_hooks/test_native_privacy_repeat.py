"""Repeat decisions through the installed chain, with owned synthetic inputs."""
import subprocess
from pathlib import Path

import pytest

from tests.test_hooks.test_native_push_privacy import CANARY, PUBLIC
from tests.test_hooks.test_native_push_privacy import native_world as native_world


def markers(repo):
    return {path.name: path.read_bytes() for path in (repo / '.git/genesis/privacy-ack').glob('*.json')}


@pytest.mark.parametrize('kind', ['portability', 'email', 'incomplete'])
def test_installed_foreground_first_repeat_and_third(native_world, kind):
    repo, env, git, commit, invoke = native_world
    text = {'portability':'10.20.30.40\n', 'email':'synthetic@owned-fixture.invalid\n', 'incomplete':'safe\n'}[kind]
    oid = commit('owned message', text=text)
    if kind == 'incomplete':
        Path(env['GENESIS_RELEASE_FINGERPRINTS']).unlink()
    results = [invoke(oid) for _ in range(3)]
    assert [result.returncode for result in results] == [2, 0, 2]
    expected = b'COULD_NOT_SCAN' if kind == 'incomplete' else b'TIER_B'
    assert expected in results[0].stderr
    assert text.strip().encode() not in results[0].stderr
    assert len(markers(repo)) == 1


@pytest.mark.parametrize('kind', ['generic', 'incomplete'])
@pytest.mark.parametrize('caller_flag', ['GENESIS_AUTOMATED_PUSH', 'GENESIS_CC_SESSION'])
def test_installed_automated_attempts_never_write_or_consume(native_world, kind, caller_flag):
    repo, env, git, commit, invoke = native_world
    oid = commit('owned message', text='10.20.30.40\n' if kind == 'generic' else 'safe\n')
    if kind == 'incomplete':
        Path(env['GENESIS_RELEASE_FINGERPRINTS']).unlink()
    env[caller_flag] = '1'
    assert [invoke(oid).returncode for _ in range(2)] == [2, 2]
    assert markers(repo) == {}
    env.pop(caller_flag)
    assert invoke(oid).returncode == 2
    before = markers(repo)
    assert len(before) == 1
    env[caller_flag] = '1'
    assert [invoke(oid).returncode for _ in range(2)] == [2, 2]
    assert markers(repo) == before
    env.pop(caller_flag)
    assert invoke(oid).returncode == 0
    assert markers(repo) == {}


@pytest.mark.parametrize('change', ['commit', 'config', 'fingerprints'])
def test_installed_changed_inputs_cannot_consume_prior_permission(native_world, change):
    repo, env, git, commit, invoke = native_world
    oid = commit('owned message', text='10.20.30.40\n')
    assert invoke(oid).returncode == 2
    before = markers(repo)
    assert len(before) == 1
    if change == 'commit':
        oid = commit('different owned message', text='10.20.30.40\n')
    elif change == 'config':
        with (Path(env['HOME']) / '.genesis/config/genesis.yaml').open('a') as stream:
            stream.write('\n# changed owned configuration identity\n')
    else:
        with Path(env['GENESIS_RELEASE_FINGERPRINTS']).open('a') as stream:
            stream.write('ANOTHER_UNMATCHED_SYNTHETIC_PATTERN\n')
    assert invoke(oid).returncode == 2
    after = markers(repo)
    assert len(after) == 2
    assert all(after[name] == content for name, content in before.items())


def test_installed_exact_hit_never_consumes_earlier_generic_permission(native_world):
    repo, env, git, commit, invoke = native_world
    generic = commit('owned generic', text='10.20.30.40\n')
    assert invoke(generic).returncode == 2
    before = markers(repo)
    exact = commit('owned exact', text=CANARY + '\n')
    results = [invoke(exact), invoke(exact)]
    assert all(result.returncode == 2 and b'TIER_A' in result.stderr for result in results)
    assert all(CANARY.encode() not in result.stdout + result.stderr for result in results)
    assert markers(repo) == before


def test_custom_rejection_precedes_permission_consumption(native_world):
    repo, env, git, commit, invoke = native_world
    hooks = repo / '.git/hooks'
    hooks.mkdir(exist_ok=True)
    original = hooks / 'pre-push'
    original.write_text('#!/bin/sh\n[ ! -f reject-owned-push ] || exit 7\n')
    original.chmod(0o755)
    installed = subprocess.run(['bash', str(repo / 'scripts/hooks/sync-hooks.sh'), '--chain-pre-push'],
                               cwd=repo, env=env, capture_output=True, timeout=10)
    assert installed.returncode == 0, installed.stderr
    oid = commit('owned generic', text='10.20.30.40\n')
    assert invoke(oid).returncode == 2
    before = markers(repo)
    refusal = repo / 'reject-owned-push'
    refusal.touch()
    assert invoke(oid).returncode == 7
    assert markers(repo) == before
    refusal.unlink()
    assert invoke(oid).returncode == 0
    assert markers(repo) == {}


def test_existing_main_branch_gate_precedes_privacy_state(native_world):
    repo, env, git, commit, invoke = native_world
    oid = commit('owned generic', text='10.20.30.40\n')
    assert invoke(oid).returncode == 2
    before = markers(repo)
    data = f'refs/heads/topic {oid} refs/heads/main {"0" * len(oid)}\n'.encode()
    result = subprocess.run([str(repo / '.git/hooks/pre-push'), 'origin', PUBLIC],
                            cwd=repo, env=env, input=data, capture_output=True, timeout=10)
    assert result.returncode == 1
    assert markers(repo) == before
