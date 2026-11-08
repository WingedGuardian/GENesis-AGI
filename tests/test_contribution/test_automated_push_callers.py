"""Automated publishers preserve environment and never bypass failed pushes."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest


def assert_env(options):
    assert options.get('env', {}).get('GENESIS_AUTOMATED_PUSH') == '1', 'publishing child lacks automated flag'
    assert options['env'].get('R9_INHERITED_SENTINEL') == 'owned', 'inherited environment lost'
    assert os.environ['GENESIS_AUTOMATED_PUSH'] == '0', 'global environment mutated'


def engine_case(engine, owned, outcome):
    async def run():
        calls = []
        proc = SimpleNamespace(returncode=1 if outcome == 'reject' else 0, communicate=AsyncMock(return_value=(b'', b'owned rejection' if outcome == 'reject' else b'ok')))
        async def spawn(*args, **kwargs):
            calls.append((args, kwargs))
            return proc
        kill, reap = Mock(), AsyncMock()
        async def stop(coro, *, timeout):
            assert timeout == engine._GIT_PUSH_TIMEOUT_S
            coro.close()
            raise TimeoutError if outcome == 'timeout' else asyncio.CancelledError
        with patch.object(engine.asyncio, 'create_subprocess_exec', spawn), patch.object(engine, 'kill_process_group', kill), patch.object(engine, 'reap_bounded', reap):
            if outcome in {'timeout', 'cancel'}:
                with patch.object(engine.asyncio, 'wait_for', stop):
                    try:
                        result = await engine._run_git_push(owned, 'task/owned')
                        assert outcome == 'timeout' and result[0] == 128
                    except asyncio.CancelledError:
                        assert outcome == 'cancel'
                kill.assert_called_once_with(proc)
                reap.assert_awaited_once_with(proc)
            else:
                result = await engine._run_git_push(owned, 'task/owned')
                assert result[0] == proc.returncode
                kill.assert_not_called()
            assert len(calls) == 1, 'unexpected retry'
            assert calls[0][0] == ('git', 'push', '-u', 'origin', 'task/owned')
            assert calls[0][1]['start_new_session'] is True
            assert_env(calls[0][1])
    asyncio.run(run())


def delivery_case(engine, owned, code):
    async def run():
        calls = []
        async def spawn(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=code, communicate=AsyncMock(return_value=(b'', b'owned result')))
        executor = object.__new__(engine.CCSessionExecutor)
        executor._worktree_paths = {'taskid': owned}
        executor._task_source = AsyncMock(return_value='build_lane')
        executor._run_scope_gate = AsyncMock(return_value=SimpleNamespace(allowed=True))
        executor._set_output = AsyncMock()
        executor._open_build_pr = AsyncMock()
        with patch.object(engine.asyncio, 'create_subprocess_exec', spawn):
            await executor._deliver('taskid', '', [{'type': 'code'}], [])
        assert len(calls) == 1
        assert executor._set_output.await_count == (0 if code else 1)
        assert executor._open_build_pr.await_count == (0 if code else 1)
        assert_env(calls[0][1])
    asyncio.run(run())


def contribution_case(opener, owned, outcome, top_level=False):
    calls = []
    def subprocess_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ['git', 'push']:
            assert kwargs['timeout'] == 120 and kwargs['check'] is False
            if outcome == 'timeout':
                raise opener.subprocess.TimeoutExpired(argv, 120)
            return SimpleNamespace(returncode=1 if outcome == 'reject' else 0, stdout='', stderr='owned push result')
        assert argv[:2] in [['git', 'worktree'], ['git', 'checkout'], ['git', 'cherry-pick'], ['git', 'ls-remote'], ['gh', 'pr']], argv
        return SimpleNamespace(returncode=0, stdout='https://github.com/fixture/public/pull/1' if argv[:2] == ['gh', 'pr'] else '', stderr='')
    with patch.object(opener.subprocess, 'run', subprocess_run), patch.object(opener, '_ensure_sha_reachable', return_value=(True, '')), patch.object(opener, 'big_tmp_dir', return_value=owned):
        if top_level:
            with patch.object(opener, 'build_pr_body', return_value='owned body'), patch.object(opener, 'branch_name', return_value='owned'), patch.object(opener, '_check_gh_available', return_value='/owned/gh'), patch.object(opener, '_check_gh_auth', return_value=(True, '')), patch.object(opener, '_gh_current_user', return_value='fixture'), patch.object(opener, '_ensure_fork', return_value=(True, '')):
                result = opener.create_pr(install=None, source_sha='a' * 40, subject='owned', version_display='owned', version_gate=None, sanitizer=SimpleNamespace(ok=True), review=None, upstream_sha='b' * 40, target_repo='fixture/public', repo_path=owned)
                assert result.ok == (outcome == 'success')
                assert sum(argv[:3] == ['gh', 'pr', 'create'] for argv, _ in calls) == (1 if outcome == 'success' else 0)
        else:
            try:
                result = opener._prepare_and_push_branch(repo_path=owned, upstream_sha='b' * 40, source_sha='a' * 40, branch='owned', fork_url='https://github.com/fixture/public.git')
                assert outcome != 'timeout' and result[0] == (outcome == 'success')
            except opener.subprocess.TimeoutExpired:
                assert outcome == 'timeout'
        pushes = [(argv, options) for argv, options in calls if argv[:2] == ['git', 'push']]
        assert len(pushes) == 1, 'unexpected publishing retry'
        assert pushes[0][0][2] == '--force-with-lease=refs/heads/owned:'
        assert_env(pushes[0][1])
        assert all('env' not in options for argv, options in calls if argv[:2] != ['git', 'push']), 'flag leaked to nonpublishing subprocess'
        assert calls[-1][0][:3] == ['git', 'worktree', 'remove'] or top_level and outcome == 'success'
        assert not list(owned.glob('genesis-contrib-*')), 'owned temporary worktree residue'



@pytest.fixture(autouse=True)
def inherited_environment(monkeypatch):
    monkeypatch.setenv("R9_INHERITED_SENTINEL", "owned")
    monkeypatch.setenv("GENESIS_AUTOMATED_PUSH", "0")

@pytest.mark.parametrize("outcome", ["success", "reject", "timeout", "cancel"])
def test_engine_push_environment_and_lifetime(tmp_path, outcome):
    from genesis.autonomy.executor import engine
    engine_case(engine, tmp_path, outcome)

@pytest.mark.parametrize("code", [0, 1])
def test_delivery_pr_requires_successful_push(tmp_path, code):
    from genesis.autonomy.executor import engine
    delivery_case(engine, tmp_path, code)

@pytest.mark.parametrize("outcome", ["success", "reject", "timeout"])
def test_contribution_push_environment_and_cleanup(tmp_path, outcome):
    from genesis.contribution import pr_opener
    contribution_case(pr_opener, tmp_path, outcome)

@pytest.mark.parametrize("outcome", ["success", "reject"])
def test_contribution_pr_requires_successful_push(tmp_path, outcome):
    from genesis.contribution import pr_opener
    contribution_case(pr_opener, tmp_path, outcome, top_level=True)
