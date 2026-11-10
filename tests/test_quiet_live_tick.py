"""Ordinary tests must not publish the install's git verdict or recovery bundle."""

from unittest.mock import AsyncMock, Mock

import pytest

from genesis.awareness import loop

_ORIGINAL_GIT_CHECK = loop._check_git_health


class _NativeActionReached(BaseException):
    """Escape best-effort Exception handlers before any real action."""


@pytest.fixture
def guarded_git_callbacks(monkeypatch):
    from genesis.awareness import loop
    from genesis.guardian import repo_bundle
    from genesis.observability import git_health
    from genesis.util import tasks

    cheap = AsyncMock(side_effect=_NativeActionReached("cheap git probe"))
    bundle = AsyncMock(side_effect=_NativeActionReached("bundle publication"))
    dispatch = Mock(side_effect=_NativeActionReached("deep git dispatch"))
    # The RED control must remain harmless even after shared test undo.
    with pytest.MonkeyPatch.context() as guards:
        guards.setattr(git_health, "check_git_cheap", cheap)
        guards.setattr(repo_bundle, "publish_repo_bundle", bundle)
        guards.setattr(tasks, "tracked_task", dispatch)
        guards.setattr(loop, "_last_bundle_publish_at", None)
        guards.setattr(loop, "_git_deep_stopped", False)
        guards.setattr(loop, "_git_deep_task", None)
        guards.setattr(loop, "_last_git_deep_run_at", None)
        monkeypatch.undo()

        async def drive():
            await loop._check_git_health(None)
            await loop._publish_repo_bundle_if_due()
            loop._dispatch_git_health_deep(None)

        yield drive, cheap, bundle, dispatch


async def test_ordinary_git_callbacks_remain_quiet_after_test_undo(guarded_git_callbacks):
    drive, cheap, bundle, dispatch = guarded_git_callbacks
    await drive()
    cheap.assert_not_awaited()
    bundle.assert_not_awaited()
    dispatch.assert_not_called()


def test_callback_override_is_temporary(monkeypatch):
    async def owned_override(*args, **kwargs):
        return None

    monkeypatch.setattr(loop, "_check_git_health", owned_override)


@pytest.mark.real_git_tick
def test_optout_after_callback_override_gets_original():
    # CI uses --dist=loadfile to keep this teardown sequence on one worker.
    # This identity check performs no native git operation even on RED.
    assert loop._check_git_health is _ORIGINAL_GIT_CHECK
