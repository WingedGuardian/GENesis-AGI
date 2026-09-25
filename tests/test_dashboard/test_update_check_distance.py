"""The distance is the DEPLOYED tree's, and a failed measurement says so.

Two properties, and the second is the one that is easy to get wrong. The count
must be `HEAD..origin/main` — the reader's own distance — rather than the span
between two release tags. And when git cannot produce that count, the endpoint
must REFUSE to answer with a number: 0 renders as "up to date" and clears a
known update, 1 fabricates a distance nobody measured. Both wear the grammar of
a measurement, which is precisely the defect this endpoint was changed to stop.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from genesis import env
from genesis.dashboard.routes import updates


def _call(count, tags):
    """Drive update_check with a stubbed git, returning (payload, status)."""

    def git(*args, **kwargs):
        if args[0] == "rev-parse":
            return "abc123"
        if args[0] == "describe":
            return tags[0] if args[-1] == "HEAD" else tags[1]
        if args[0] == "rev-list":
            # The range itself is the fix. Asserted here rather than in a value
            # comparison, because a tag-span implementation would still return a
            # plausible number — it is the ARGUMENTS that distinguish them.
            assert args == ("rev-list", "--count", "HEAD..abc123")
            return count
        if args[0] == "log":
            assert args[-1] == "HEAD..abc123"
            return "two\none"
        return ""

    def git_result(*args, **kwargs):
        assert args == (
            "fetch",
            "origin",
            "+refs/heads/main:refs/genesis-update-check",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        return "", ""

    app = Flask(__name__)
    with app.test_request_context(), patch.object(
        updates, "_deploy_target", return_value=("origin", "main")
    ), patch.object(
        updates, "_git", side_effect=git
    ), patch.object(updates, "_git_result", side_effect=git_result):
        result = updates.update_check()

    if isinstance(result, tuple):
        response, status = result
        return response.get_json(), status
    return result.get_json(), 200


@pytest.mark.parametrize("count", ["0", "2", "20"])
@pytest.mark.parametrize("tags", [("v1", "v2"), (None, "v2"), (None, None)])
def test_a_measured_distance_is_reported_as_measured(count, tags):
    payload, status = _call(count, tags)
    assert status == 200
    assert payload["commits_behind"] == int(count)
    assert payload["summary"] == ("two\none" if int(count) else None)


@pytest.mark.parametrize("count", [None, "", "invalid"])
@pytest.mark.parametrize("tags", [("v1", "v2"), (None, "v2"), (None, None)])
def test_an_unmeasurable_distance_is_an_error_not_a_number(count, tags):
    # The whole point: NO tag combination may turn an unreadable count into a
    # number. The previous behaviour returned 1 when both tags were present and
    # 0 otherwise — and that 0 cleared a real update from the dashboard while
    # claiming the install was current.
    payload, status = _call(count, tags)
    assert status == 502
    assert "commits_behind" not in payload
    assert "error" in payload


def test_deploy_target_uses_the_shared_resolver(monkeypatch):
    calls = []

    def resolve(repo):
        calls.append(repo)
        return "upstream", "release"

    monkeypatch.setattr(updates, "deploy_target", resolve)
    assert updates._deploy_target() == ("upstream", "release")
    assert calls == [updates._GENESIS_ROOT]


def test_shared_deploy_target_uses_public_repo_remote_and_env_branch(
    monkeypatch, tmp_path,
):
    def run(cmd, **kwargs):
        if cmd[3:] == ["remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin https://github.com/other/repo.git (fetch)\n"
                    "upstream https://github.com/WingedGuardian/GENesis-AGI.git (fetch)"
                ),
                stderr="",
            )
        if "check-ref-format" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setenv("GENESIS_DEPLOY_BRANCH", "release")
    monkeypatch.setattr(env, "github_public_repo", lambda: "GENesis-AGI")
    monkeypatch.setattr(env.subprocess, "run", run)

    assert env.deploy_target(tmp_path) == ("upstream", "release")


def test_shared_deploy_target_refreshes_the_live_remote_head(monkeypatch, tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[3:])
        if cmd[3:] == ["remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout="origin https://github.com/WingedGuardian/GENesis-AGI.git (fetch)",
                stderr="",
            )
        if cmd[3:] == ["remote", "set-head", "--auto", "origin"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[3:5] == ["symbolic-ref", "--quiet"]:
            return SimpleNamespace(returncode=0, stdout="origin/release\n", stderr="")
        if "check-ref-format" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.delenv("GENESIS_DEPLOY_BRANCH", raising=False)
    monkeypatch.setattr(env, "github_public_repo", lambda: "GENesis-AGI")
    monkeypatch.setattr(env.subprocess, "run", run)

    assert env.deploy_target(tmp_path) == ("origin", "release")
    assert ["remote", "set-head", "--auto", "origin"] in calls


def test_same_release_preserves_the_existing_update_policy():
    def git(*args, **kwargs):
        assert args[0] in ("remote", "symbolic-ref", "rev-parse", "fetch", "describe")
        if args[0] == "rev-parse":
            return "abc123"
        return "v1" if args[0] == "describe" else ""

    app = Flask(__name__)
    with app.test_request_context(), patch.object(
        updates, "_deploy_target", return_value=("origin", "main")
    ), patch.object(
        updates, "_git", side_effect=git
    ), patch.object(updates, "_git_result", return_value=("", "")):
        result = updates.update_check().get_json()
    assert result["commits_behind"] == 0
    assert result["target_tag"] is None
