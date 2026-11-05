"""Comments are skipped the way bash skips them (#3473), checked against real bash.

Each case runs under the owned-shim oracle, which records every program bash
actually executed, and then through ``analyze_checked``. Two properties:

- a command on the line after a comment is SEEN, whatever the comment holds (a
  quote, a backtick, a backslash, an operator): before the fix a quote inside a
  comment opened scanner state that swallowed the executed lines after it;
- text inside a comment is NOT reported as a command, because bash never runs it.

A ``#`` that does not start a word (mid-word, ``$#``, ``${#v}``, after an escaped
space) is ordinary text, and what follows it on the line still runs.
"""

import pytest

from tests.test_hooks.test_shell_parse_bash_oracle import sp
from tests.test_hooks.test_unquoted_escape_boundaries import owned_oracle  # noqa: F401

_CURL = ("curl", "__genesis_oracle_7__", "x")


def _seen(command: str) -> bool:
    segments, blind = sp.analyze_checked(command)
    return blind is not None or any(tuple(s.argv) == _CURL for s in segments)


def _reported(command: str) -> bool:
    segments, _blind = sp.analyze_checked(command)
    return any(tuple(s.argv) == _CURL for s in segments)


@pytest.mark.parametrize("payload", ["it's", '"', "`", "\\", "$(", "'a", "; ls", "&& ls", "|"])
@pytest.mark.parametrize(
    "lead", ["", "ls __genesis_oracle_1__ ", "ls __genesis_oracle_1__;", "ls a |", "ls a &&"]
)
def test_the_line_after_a_comment_is_seen(owned_oracle, lead, payload):  # noqa: F811
    command = f"{lead}# {payload}\ncurl __genesis_oracle_7__ x\n# {payload}"
    assert _CURL in owned_oracle.run(command)
    assert _seen(command), command


@pytest.mark.parametrize("before", [" ", "\t", ";", "|", "&&", "\n"])
def test_text_inside_a_comment_is_not_a_command(owned_oracle, before):  # noqa: F811
    command = f"ls __genesis_oracle_1__{before}# ; curl __genesis_oracle_7__ x"
    assert _CURL not in owned_oracle.run(command)
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert not _reported(command), [s.argv for s in segments]


@pytest.mark.parametrize(
    "command",
    [
        "ls a#b; curl __genesis_oracle_7__ x",
        "ls $#; curl __genesis_oracle_7__ x",
        "v=a; ls ${#v}; curl __genesis_oracle_7__ x",
        "ls \\ #b; curl __genesis_oracle_7__ x",
        "ls \\\\\\ #b; curl __genesis_oracle_7__ x",
    ],
)
def test_a_hash_that_does_not_start_a_word_is_text(owned_oracle, command):  # noqa: F811
    assert _CURL in owned_oracle.run(command)
    assert _reported(command), command


def test_an_escaped_backslash_before_the_space_still_opens_a_comment(owned_oracle):  # noqa: F811
    command = "ls \\\\ #b; curl __genesis_oracle_7__ x"
    assert _CURL not in owned_oracle.run(command)
    assert not _reported(command)


def test_a_trailing_backslash_does_not_continue_a_comment(owned_oracle):  # noqa: F811
    command = "ls a # x \\\ncurl __genesis_oracle_7__ x"
    assert _CURL in owned_oracle.run(command)
    assert _seen(command)


def test_a_comment_inside_a_nested_bash_c_script_is_skipped_there(owned_oracle):  # noqa: F811
    body = "f() {  # a|b, curl __genesis_oracle_7__ x\n  ls __genesis_oracle_1__\n}\nf"
    command = "bash -c '" + body + "'"
    assert _CURL not in owned_oracle.run(command)
    assert not _reported(command)


@pytest.mark.parametrize(
    "lead",
    [
        "ls ${x:- #}; ",
        "ls `ls a #`; ",
        "ls ${x// #/y}; ",
        "ls ${x:-\n#}; ",
        "ls `ls a\n#`; ",
        'ls ${x:- "}" #}; ',
        "ls >${x:- #}; ",
        # A stray closer must not cancel a later opener (security review).
        "ls ] ${x:- #}; ",
        "ls } ${x:- #}; ",
        "ls ${x:- ) #}; ",
        "case a in a) :;; esac; ls ${x:- #}; ",
    ],
)
def test_a_hash_inside_an_open_expansion_or_bracket_is_not_a_comment(owned_oracle, lead):  # noqa: F811
    """``parse_segments`` tracks no ``${``/backtick/``[[`` nesting; there the ``#``
    is text to bash, and skipping it hid the next command (review BLOCKER)."""
    command = lead + "curl __genesis_oracle_7__ x"
    assert _CURL in owned_oracle.run(command)
    assert _seen(command), command


def test_override_sigils_in_a_comment_stay_in_raw():
    segments, blind = sp.analyze_checked("git push # review-override")
    assert blind is None
    (segment,) = [s for s in segments if s.argv]
    assert segment.argv == ["git", "push"]
    assert "# review-override" in segment.raw


def test_a_command_with_a_heredoc_keeps_the_legacy_scan():
    """Comment skipping is off when ``<<`` appears (here-doc bodies are data)."""
    command = "cat <<EOF\n# it's\nEOF\ncurl __genesis_oracle_7__ x"
    assert _seen(command)
