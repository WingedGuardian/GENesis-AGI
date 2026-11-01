"""The partial word repair must not reinterpret quotes inside legacy comments."""

import shlex

import pytest

from tests.test_hooks.test_shell_parse_bash_oracle import sp
from tests.test_hooks.test_unquoted_escape_boundaries import owned_oracle as owned_oracle


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("slashes", [1, 2, 3, 4])
@pytest.mark.parametrize("prefix", ["echo before ", "true; "])
@pytest.mark.parametrize("nested", [False, True])
def test_comment_quotes_cannot_newly_hide_executed_suffix(owned_oracle, quote, slashes, prefix, nested):
    body = prefix + "# " + "\\" * slashes + quote + "x" + quote
    body += "\ncurl __genesis_oracle_999__ x\necho after # " + quote
    command = "bash -c " + shlex.quote(body) if nested else body
    observed = owned_oracle.run(command)
    assert ("curl", "__genesis_oracle_999__", "x") in observed
    segments, blind = sp.analyze_checked(command)
    assert blind is not None or any(
        s.argv == ["curl", "__genesis_oracle_999__", "x"] for s in segments
    )
