"""The distance is the DEPLOYED tree's, and a failed measurement says so.

Two properties, and the second is the one that is easy to get wrong. The count
must be `HEAD..origin/main` — the reader's own distance — rather than the span
between two release tags. And when git cannot produce that count, the endpoint
must REFUSE to answer with a number: 0 renders as "up to date" and clears a
known update, 1 fabricates a distance nobody measured. Both wear the grammar of
a measurement, which is precisely the defect this endpoint was changed to stop.
"""

from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard.routes import updates


def _call(count, tags, remote="origin"):
    """Drive update_check with a stubbed git, returning (payload, status)."""

    def git(*args, **kwargs):
        if args[0] == "describe":
            return tags[0] if args[-1] == "HEAD" else tags[1]
        if args[0] == "rev-list":
            # The range itself is the fix. Asserted here rather than in a value
            # comparison, because a tag-span implementation would still return a
            # plausible number — it is the ARGUMENTS that distinguish them.
            assert args == ("rev-list", "--count", f"HEAD..refs/remotes/{remote}/main")
            return count
        if args[0] == "log":
            assert args[-1] == f"HEAD..refs/remotes/{remote}/main"
            return "two\none"
        return ""

    app = Flask(__name__)
    with app.test_request_context(), patch.object(
        updates, "_git", side_effect=git
    ), patch.object(updates, "_git_result", return_value=("", "")), patch(
        "genesis.util.update_remote.update_remote", return_value=remote
    ):
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


def test_same_release_preserves_the_existing_update_policy():
    def git(*args, **kwargs):
        assert args[0] in ("fetch", "describe")
        return "v1" if args[0] == "describe" else ""

    app = Flask(__name__)
    with app.test_request_context(), patch.object(
        updates, "_git", side_effect=git
    ), patch.object(updates, "_git_result", return_value=("", "")), patch(
        "genesis.util.update_remote.update_remote", return_value="origin"
    ):
        result = updates.update_check().get_json()
    assert result["commits_behind"] == 0
    assert result["target_tag"] is None



def test_the_check_reads_the_remote_updates_use():
    """A pinned non-origin remote is what update.sh installs from, so the
    manual check measures against it, not origin (Devin, round 1 on #2726)."""
    payload, status = _call("3", (None, None), remote="public")
    assert status == 200
    assert payload["commits_behind"] == 3


def test_a_refused_remote_choice_is_a_failed_check_not_a_number():
    from genesis.util.update_remote import UpdateRemoteError

    app = Flask(__name__)
    with app.test_request_context(), patch(
        "genesis.util.update_remote.update_remote",
        side_effect=UpdateRemoteError("ambiguous"),
    ):
        response, status = updates.update_check()
    assert status == 502
    assert "cannot choose the update remote" in response.get_json()["error"]



def test_the_check_fetches_into_the_ref_it_reads(tmp_path):
    """A remote whose configured refspec does not track main (`git remote add -t
    other`): a bare `git fetch <remote> main` would leave refs/remotes/<r>/main
    absent, and the check would fail on a remote update.sh can update from."""
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
    )

    def g(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)

    upstream, checkout = tmp_path / "upstream", tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True, env=env)
    g(upstream, "commit", "-q", "--allow-empty", "-m", "a")
    subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True, env=env)
    g(upstream, "commit", "-q", "--allow-empty", "-m", "b")
    g(checkout, "remote", "add", "-t", "other", "public", str(upstream))

    app = Flask(__name__)
    with (
        app.test_request_context(),
        patch.object(updates, "_GENESIS_ROOT", checkout),
        patch("genesis.util.update_remote.update_remote", return_value="public"),
    ):
        result = updates.update_check()
    response, status = result if isinstance(result, tuple) else (result, 200)
    assert status == 200, response.get_json()
    assert response.get_json()["commits_behind"] == 1
