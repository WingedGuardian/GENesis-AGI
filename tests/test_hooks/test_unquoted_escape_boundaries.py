"""Real Bash witnesses for escaped words; all external programs are owned shims."""

import shlex

import pytest

from tests.test_hooks.test_shell_parse_bash_oracle import (
    _BashOracle,
    _prepare_work_dir,
    _write_shims,
    sp,
)


@pytest.fixture
def owned_oracle(tmp_path):
    programs = tmp_path / "bin"
    programs.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _write_shims(programs)
    _prepare_work_dir(work)
    return _BashOracle(programs, work, home)


@pytest.mark.parametrize(
    "prefix", [r"ls It\'s", r"ls q\"r", r"ls 'a'\''b'", r"ls a\\b", r"ls a\;b", r"ls a\#b"]
)
@pytest.mark.parametrize("separator", ["; ", "\n", " && ", " || ", " | ", " & "])
@pytest.mark.parametrize("nested", [False, True])
def test_escaped_prefix_never_swallows_executed_suffix(owned_oracle, prefix, separator, nested):
    body = (
        prefix
        + ("; false" if separator == " || " else "")
        + separator
        + "curl __genesis_oracle_999__ x"
    )
    if separator == " & ":
        body += "; wait"
    command = "bash -c " + shlex.quote(body) if nested else body
    observed = owned_oracle.run(command)
    assert ("curl", "__genesis_oracle_999__", "x") in observed
    segments, blind = sp.analyze_checked(command)
    assert blind is not None or any(
        s.argv == ["curl", "__genesis_oracle_999__", "x"] for s in segments
    )


@pytest.mark.parametrize(
    "char", ["'", '"', "\\", ";", "&", "|", "<", ">", "#", "$", "`", " ", "(", ")"]
)
def test_escaped_character_remains_word_data(owned_oracle, char):
    command = "ls __genesis_oracle_1__ a\\" + char + "b; curl __genesis_oracle_2__"
    observed = owned_oracle.run(command)
    assert ("ls", "__genesis_oracle_1__", "a" + char + "b") in observed
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert [s.argv for s in segments] == [
        ["ls", "__genesis_oracle_1__", "a" + char + "b"],
        ["curl", "__genesis_oracle_2__"],
    ]
