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
        if args[0] == "fetch":
            # Three distinct fetches, and only the FIRST may be fatal. The
            # tracking ref and the tags are conveniences that fail for reasons
            # unrelated to the deploy head — a stale ancestor blocking its own
            # descendant, a rewritten upstream tag — and bundling either into the
            # measuring fetch turned a good measurement into a 502.
            if len(args) > 2 and ":refs/genesis-update-check/" in args[2]:
                check_ref = args[2].split(":", 1)[1]
                assert check_ref.startswith("refs/genesis-update-check/dashboard/")
                assert args == ("fetch", "origin", f"+refs/heads/main:{check_ref}"), (
                    "the measuring fetch must carry the private ref ALONE"
                )
            elif "--tags" in args:
                assert "--force" in args, "a rewritten tag must not fail the sync"
            else:
                assert args == (
                    "fetch",
                    "origin",
                    "+refs/heads/main:refs/remotes/origin/main",
                ), f"unexpected fetch: {args}"
        elif args[0] == "update-ref":
            assert args[:2] == ("update-ref", "-d")
            assert args[2].startswith("refs/genesis-update-check/dashboard/")
        else:
            raise AssertionError(f"unexpected git call: {args}")
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
@pytest.mark.parametrize(
    "tags", [("v1", "v2"), ("v1", "v1"), (None, "v2"), (None, None)]
)
def test_a_measured_distance_is_reported_as_measured(count, tags):
    payload, status = _call(count, tags)
    assert status == 200
    assert payload["commits_behind"] == int(count)
    assert payload["summary"] == ("two\none" if int(count) else None)


@pytest.mark.parametrize("count", [None, "", "invalid"])
@pytest.mark.parametrize(
    "tags", [("v1", "v2"), ("v1", "v1"), (None, "v2"), (None, None)]
)
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


def test_shared_deploy_target_uses_public_repo_remote_and_configured_branch(
    monkeypatch, tmp_path,
):
    def run(cmd, **kwargs):
        if cmd[3:] == ["remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin https://github.com/WingedGuardian/GENesis-AGI-backup.git (fetch)\n"
                    "upstream https://github.com/WingedGuardian/GENesis-AGI.git (fetch)"
                ),
                stderr="",
            )
        if "check-ref-format" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(env, "deploy_branch_override", lambda: "release")
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
        if cmd[3:] == ["ls-remote", "--symref", "origin", "HEAD"]:
            return SimpleNamespace(
                returncode=0,
                stdout="ref: refs/heads/release\tHEAD\nabc123\tHEAD",
                stderr="",
            )
        if cmd[3:] == [
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/release",
        ]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[3:5] == ["symbolic-ref", "--quiet"]:
            return SimpleNamespace(returncode=0, stdout="origin/release\n", stderr="")
        if "check-ref-format" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(env, "deploy_branch_override", lambda: "")
    monkeypatch.setattr(env, "github_public_repo", lambda: "GENesis-AGI")
    monkeypatch.setattr(env.subprocess, "run", run)

    assert env.deploy_target(tmp_path) == ("origin", "release")
    assert ["ls-remote", "--symref", "origin", "HEAD"] in calls


def test_shared_deploy_target_local_mode_uses_cached_remote_head(
    monkeypatch, tmp_path,
):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[3:])
        if cmd[3:] == ["remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout="origin\thttps://example.test/WingedGuardian/GENesis-AGI.git (fetch)\n",
                stderr="",
            )
        if cmd[3:5] == ["symbolic-ref", "--quiet"]:
            return SimpleNamespace(returncode=0, stdout="origin/release\n", stderr="")
        if "check-ref-format" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(env, "deploy_branch_override", lambda: "")
    monkeypatch.setattr(env, "github_public_repo", lambda: "GENesis-AGI")
    monkeypatch.setattr(env.subprocess, "run", run)

    assert env.deploy_target(tmp_path, probe_remote_head=False) == ("origin", "release")
    assert ["ls-remote", "--symref", "origin", "HEAD"] not in calls


def test_invalid_deploy_target_does_not_expose_exception_text():
    app = Flask(__name__)
    with app.test_request_context(), patch.object(
        updates, "_deploy_target", side_effect=ValueError("secret path detail")
    ):
        response, status = updates.update_check()

    assert status == 400
    assert response.get_json() == {"error": "invalid deploy target"}


def test_same_release_preserves_the_existing_update_policy():
    def git(*args, **kwargs):
        assert args[0] in (
            "remote", "symbolic-ref", "rev-parse", "fetch", "describe", "rev-list", "log"
        )
        if args[0] == "rev-parse":
            return "abc123"
        if args[0] == "rev-list":
            return "0"
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
