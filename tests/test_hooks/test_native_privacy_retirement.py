"""Final installed profiles and actual shell repository selection, owned only."""
import hashlib
import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_hooks.test_native_push_privacy import CANARY, _record, _worker
from tests.test_hooks.test_native_push_privacy import native_world as native_world
from tests.test_hooks.test_privacy_automated_publication import (
    publication_world as publication_world,
)


@pytest.mark.parametrize('profile', ['default', 'relative', 'absolute', 'linked', 'separate', 'dev-local'])
@pytest.mark.parametrize('hit', [False, True])
def test_installed_supported_profiles_publish_only_clean(publication_world, record_property, profile, hit):
    repo, env, git, commit, base, refs = publication_world
    source = repo
    install_options = []
    if profile in ('linked', 'dev-local'):
        source = repo.parent / 'linked'
        git('worktree', 'add', '--detach', str(source), base)
        for name in ('scripts', 'src', '.venv'):
            (source / name).symlink_to(repo / name, target_is_directory=True)
        git('config', 'core.hooksPath', 'owned-hooks')
        if profile == 'dev-local':
            env['GENESIS_HOOK_DEV_LOCAL'] = '1'
    elif profile == 'separate':
        # This profile installs into separate metadata from the start. Moving
        # an enrolled manifest would instead test forbidden stale ownership.
        restored = subprocess.run(['bash', str(repo / 'scripts/hooks/sync-hooks.sh'), '--restore-pre-push'],
                                  cwd=repo, env=env, capture_output=True, timeout=10)
        assert restored.returncode == 0, restored.stderr
        git('init', '--separate-git-dir', str(repo.parent / 'metadata'))
        install_options = ['--chain-pre-push']
    elif profile in ('relative', 'absolute'):
        git('config', 'core.hooksPath', str(repo / 'owned-hooks') if profile == 'absolute' else 'owned-hooks')
    installed = subprocess.run(['bash', str(repo / 'scripts/hooks/sync-hooks.sh'), '--hook-repo', str(source), *install_options],
                               cwd=repo, env=env, capture_output=True, timeout=10)
    assert installed.returncode == 0, installed.stderr
    tip = commit('owned final qualification', parent=base, text=CANARY + '\n' if hit else 'owned clean\n')
    git('update-ref', 'refs/heads/qualification', tip)
    before = refs()
    result = subprocess.run(['git', '-C', str(source), 'push', 'origin', 'qualification'],
                            env=env, capture_output=True, timeout=70)
    assert (result.returncode != 0) is hit, result.stderr
    assert (refs() == before) is hit
    assert CANARY.encode() not in result.stdout + result.stderr
    if hit:
        assert b'TIER_A' in result.stderr
    path = subprocess.run(['git', '-C', str(source), 'rev-parse', '--path-format=absolute', '--git-path', 'hooks'],
                          env=env, capture_output=True, timeout=5, check=True).stdout.removesuffix(b'\n')
    hook_dir = Path(path.decode())
    record_property('installed_profile', json.dumps({
        'profile': profile, 'hit': hit, 'exit': result.returncode,
        'hook_path': str(hook_dir), 'source': str(source),
        'helpers': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in hook_dir.iterdir()
                    if p.is_file() and (p.name.endswith('.py') or p.name == 'pre-push')},
    }, sort_keys=True))


@pytest.mark.parametrize('form', ['cd', 'git-C', 'env-S', 'literal-tilde', 'expanded-home',
                                  'subshell', 'git-environment', 'failed-cd',
                                  'physical-parent', 'failed-cd-semicolon'])
def test_real_shell_target_cannot_be_replaced_by_clean_decoy(publication_world, form):
    repo, env, git, commit, base, refs = publication_world
    tip = commit('owned shell qualification', parent=base, text=CANARY + '\n')
    git('update-ref', 'refs/heads/qualification', tip)
    decoy = repo.parent / 'decoy'
    decoy.mkdir()
    subprocess.run(['git', '-c', 'init.templateDir=', 'init', '-q', str(decoy)],
                   env=env, check=True, capture_output=True, timeout=5)
    (decoy / '~').symlink_to(repo, target_is_directory=True)
    (repo / 'child').mkdir()
    (decoy / 'link').symlink_to(repo / 'child', target_is_directory=True)
    (decoy / 'clean.txt').write_text('owned clean lexical parent\n')
    q = shlex.quote(str(repo))
    commands = {
        'cd': f'cd {q} && git push origin qualification',
        'git-C': f'git -C {q} push origin qualification',
        'env-S': 'env -S ' + shlex.quote(f'git -C {q} push origin qualification'),
        'literal-tilde': "cd '~' && git push origin qualification",
        'expanded-home': 'git -C "$HOME/repo" push origin qualification',
        'subshell': f'(cd {q} && git push origin qualification)',
        'git-environment': f'GIT_DIR={q}/.git GIT_WORK_TREE={q} git push origin qualification',
        'failed-cd': 'cd owned-missing-directory && git push origin qualification',
        'physical-parent': 'git -C link/.. push origin qualification',
        'failed-cd-semicolon': 'cd owned-missing-directory; git push origin qualification',
    }
    if form == 'physical-parent':
        physical = subprocess.run(['git', '-C', 'link/..', 'rev-parse', '--show-toplevel'],
                                  cwd=decoy, env=env, check=True, capture_output=True, timeout=5)
        assert Path(physical.stdout.decode().removesuffix('\n')) == repo
        assert (decoy / 'link/..').resolve() == repo
        assert CANARY not in (decoy / 'clean.txt').read_text()
    before = refs()
    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', commands[form]],
                            cwd=repo if form == 'failed-cd-semicolon' else decoy,
                            env=env, capture_output=True, timeout=70)
    assert result.returncode != 0
    assert refs() == before
    assert CANARY.encode() not in result.stdout + result.stderr
    if form != 'failed-cd':
        assert b'TIER_A' in result.stderr, result.stderr


@pytest.mark.parametrize('flag', ['GENESIS_AUTOMATED_PUSH', 'GENESIS_CC_SESSION'])
@pytest.mark.parametrize('mode', ['0', '1'])
@pytest.mark.parametrize('shape', ['aws', 'url'])
def test_credential_a_preserves_even_matching_prior_permit(native_world, monkeypatch, flag, mode, shape):
    from scripts.hooks import push_privacy_ack as ack
    from scripts.hooks import push_privacy_scan as scan
    from tests.test_contribution.test_credentials import _key, _url

    repo, env, git, commit, invoke = native_world
    value = _key('ASIA') if shape == 'aws' else _url('one:two')
    oid = commit('owned credential', text=value + '\n')
    payload = _worker(native_world, _record(oid))
    assert payload['verdict'] == 'TIER_A'
    assert {finding[0] for finding in payload['findings']} == {
        'aws-key-id' if shape == 'aws' else 'url-credential'}
    result = scan.ScanResult(**payload)
    monkeypatch.setitem(sys.modules, 'push_privacy_ack', ack)
    for name in ('GENESIS_AUTOMATED_PUSH', 'GENESIS_CC_SESSION'):
        monkeypatch.setenv(name, '0')
        env[name] = '0'
    # Deliberately seed an owned Tier B permit for the exact credential token.
    # This impossible normal transition tests the policy boundary, not an
    # assumption that different verdicts necessarily have different tokens.
    assert scan.finish(replace(result, verdict='TIER_B')) == 2
    marker = repo / '.git/genesis/privacy-ack' / (result.identity + '.json')
    before = marker.read_bytes()
    monkeypatch.setenv(flag, mode)
    env[flag] = mode
    with monkeypatch.context() as guard:
        guard.setattr(ack, 'acknowledge', lambda *a, **kw: pytest.fail('Tier A consulted repeat state'))
        assert scan.finish(result) == 2
    for _ in range(2):
        refused = invoke(oid)
        assert refused.returncode == 2 and b'TIER_A' in refused.stderr
        assert value.encode() not in refused.stdout + refused.stderr
        assert marker.read_bytes() == before
