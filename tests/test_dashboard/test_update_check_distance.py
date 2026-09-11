"""A failed distance measurement must retain the differing-release warning."""

from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard.routes import updates


@pytest.mark.parametrize("count", [None, "", "invalid", "0", "2"])
@pytest.mark.parametrize("tags", [("v1", "v2"), (None, "v2"), (None, None)])
def test_distance_preserves_the_existing_failure_fallback(count, tags):
    def git(*args, **kwargs):
        if args[0] == "describe":
            return tags[0] if args[-1] == "HEAD" else tags[1]
        if args[0] == "rev-list":
            assert args == ("rev-list", "--count", "HEAD..origin/main")
            return count
        if args[0] == "log":
            assert args[-1] == "HEAD..origin/main"
            return "two\none"
        return ""

    app = Flask(__name__)
    with app.test_request_context(), patch.object(updates, "_git", side_effect=git), patch.object(
        updates, "_git_result", return_value=("", "")
    ):
        result = updates.update_check().get_json()

    expected = int(count) if count and count.isdigit() else (1 if all(tags) else 0)
    assert result["commits_behind"] == expected
    assert result["summary"] == ("two\none" if expected else None)


def test_same_release_preserves_the_existing_update_policy():
    def git(*args, **kwargs):
        assert args[0] in ("fetch", "describe")
        return "v1" if args[0] == "describe" else ""

    app = Flask(__name__)
    with app.test_request_context(), patch.object(updates, "_git", side_effect=git), patch.object(
        updates, "_git_result", return_value=("", "")
    ):
        result = updates.update_check().get_json()
    assert result["commits_behind"] == 0
    assert result["target_tag"] is None
