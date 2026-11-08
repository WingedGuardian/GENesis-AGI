"""Actual publishing callers, installed hooks and local-only Git destinations."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_hooks.test_native_privacy_repeat import markers
from tests.test_hooks.test_native_push_privacy import PUBLIC
from tests.test_hooks.test_native_push_privacy import native_world as native_world


@pytest.fixture
def publication_world(native_world, monkeypatch):
    repo, env, git, commit, invoke = native_world
    root = repo.parent
    remote = root / 'public.git'
    subprocess.run(['git', '-c', 'init.templateDir=', 'init', '--bare', '-q', str(remote)],
                   env=env, check=True, capture_output=True, timeout=5)
    # Historical generic content is already public. Only newly published
    # generic content should stop a routine automated branch.
    base = commit('owned public baseline', text='10.20.30.40\n')
    git('update-ref', 'refs/heads/main', base)
    git('reset', '--hard', base)
    git('-c', 'protocol.file.allow=always', 'push', str(remote), 'main')
    shim = root / 'owned-local-ssh'
    shutil.copy2(Path(__file__).with_name('_privacy_local_ssh.py'), shim)
    shim.chmod(0o700)
    env.update(GIT_SSH=str(shim), GIT_SSH_VARIANT='ssh', R8_LOCAL_WORLD=str(root),
               GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_1='protocol.ssh.allow', GIT_CONFIG_VALUE_1='always',
               GENESIS_AUTOMATED_PUSH='0', R9_INHERITED_SENTINEL='owned')
    git('remote', 'add', 'origin', PUBLIC)
    # A pre-existing original records only selected synthetic metadata. Its
    # output proves the real publishing child inherited the caller's flag.
    original = repo / '.git/hooks/pre-push'
    original.parent.mkdir(exist_ok=True)
    original.write_text('#!/usr/bin/python3\nimport json, os, sys\nfrom pathlib import Path\n'
                        'root = Path(os.environ["R8_LOCAL_WORLD"])\n'
                        'with (root / "original.jsonl").open("a") as stream:\n'
                        '    stream.write(json.dumps({"argv":sys.argv[1:], "stdin":sys.stdin.read(), '
                        '"automated":os.getenv("GENESIS_AUTOMATED_PUSH"), '
                        '"inherited":os.getenv("R9_INHERITED_SENTINEL")}) + "\\n")\n')
    original.chmod(0o755)
    installed = subprocess.run(['bash', str(repo / 'scripts/hooks/sync-hooks.sh'), '--chain-pre-push'],
                               cwd=repo, env=env, capture_output=True, timeout=10)
    assert installed.returncode == 0, installed.stderr
    # The real callers inherit process environment, not a subprocess stub.
    for key in list(os.environ):
        if key.startswith('GIT_') or key.startswith('GENESIS_'):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def refs():
        result = subprocess.run(['git', '--git-dir', str(remote), 'show-ref'],
                                env=env, capture_output=True, check=True, timeout=5)
        return result.stdout

    return repo, env, git, commit, base, refs


@pytest.mark.parametrize('caller', ['executor', 'contribution'])
@pytest.mark.parametrize('kind', ['clean', 'generic', 'incomplete'])
def test_actual_automated_publishers(publication_world, monkeypatch, caller, kind):
    repo, env, git, commit, base, refs = publication_world
    tip = commit('owned new change', parent=base,
                 text='10.30.40.50\n' if kind == 'generic' else 'owned safe content\n')
    branch = 'task/owned'
    git('update-ref', 'refs/heads/' + branch, tip)
    if kind == 'incomplete':
        Path(env['GENESIS_RELEASE_FINGERPRINTS']).unlink()
    before = refs()
    outcomes = []
    for _ in range(2):
        if caller == 'executor':
            from genesis.autonomy.executor import engine

            code, message = asyncio.run(engine._run_git_push(repo, branch))
            ok = code == 0
        else:
            from genesis.contribution import pr_opener

            monkeypatch.setattr(pr_opener, 'big_tmp_dir', lambda: repo.parent)
            ok, message = pr_opener._prepare_and_push_branch(
                repo_path=repo, upstream_sha=base, source_sha=tip,
                branch='contribution/owned', fork_url=PUBLIC,
            )
        outcomes.append(ok)
        assert '10.30.40.50' not in message
        assert markers(repo) == {}, 'automated publication created or consumed a permit'
    assert outcomes == ([True, True] if kind == 'clean' else [False, False])
    assert (refs() != before) == (kind == 'clean'), 'unexpected destination ref mutation'
    if kind != 'clean':
        assert ('TIER_B' if kind == 'generic' else 'COULD_NOT_SCAN') in message
    records = [json.loads(line) for line in (repo.parent / 'original.jsonl').read_text().splitlines()]
    assert records and all(row['automated'] == '1' and row['inherited'] == 'owned' for row in records)
    assert all(row['argv'][1] == PUBLIC for row in records)
    transport = [json.loads(line) for line in (repo.parent / 'transport.jsonl').read_text().splitlines()]
    assert all(row['allowed'] for row in transport)
    assert any(row['exec'][0] == '/usr/bin/git-receive-pack' for row in transport)
    assert not list(repo.parent.glob('genesis-contrib-*')), 'temporary contribution worktree survived'


def test_native_foreground_refusal_repeat_and_publication(publication_world):
    repo, env, git, commit, base, refs = publication_world
    tip = commit('owned generic change', parent=base, text='10.30.40.50\n')
    git('update-ref', 'refs/heads/topic', tip)
    before = refs()
    command = ['git', '-C', str(repo), 'push', 'origin', 'topic']
    first = subprocess.run(command, env=env, capture_output=True, timeout=70)
    assert first.returncode != 0 and b'TIER_B' in first.stderr
    assert refs() == before and len(markers(repo)) == 1
    second = subprocess.run(command, env=env, capture_output=True, timeout=70)
    assert second.returncode == 0, second.stderr
    assert refs() != before and markers(repo) == {}
    assert b'10.30.40.50' not in first.stdout + first.stderr + second.stdout + second.stderr
