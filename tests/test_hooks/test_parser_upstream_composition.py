"""The shared nested-analysis repair must retain current-main heredoc safety.

All strings are parser input only. No submitted command body executes here.
"""
import shlex
from pathlib import Path

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = 'composition_probe tail'


@pytest.fixture
def parser(monkeypatch):
    path = ROOT / 'scripts/hooks/shell_parse.py'
    monkeypatch.syspath_prepend(str(path.parent))
    return private_module('composition_parser', path)


@pytest.mark.parametrize('head,terminator', [
    ("tee fixture <<'EOF'", 'EOF'), ("cat > fixture <<'EOF'", 'EOF'),
    ("python3 - <<'PY'", 'PY'), ("cat <<-'EOF'", 'EOF'),
])
def test_reconciliation_preserves_main_hidden_heredoc_refusal(parser, head, terminator):
    command = head + '\nx<`\n' + terminator + '\n' + SUFFIX
    segments, blind = parser.analyze_checked(command)
    assert blind is not None and segments == []
    assert parser.rewrites_before_running(command)


@pytest.mark.parametrize('separator', ['; ', '\n', ' && '])
def test_reconciliation_preserves_parent_nested_uncertainty(parser, separator):
    body = "echo \"$(echo 'a\"b')\"" + separator + SUFFIX
    command = 'bash -c ' + shlex.quote(body)
    segments, blind = parser.analyze_checked(command)
    assert blind is not None or any(segment.exe == 'composition_probe' for segment in segments)


def test_combined_nested_and_heredoc_facts_remain_independent(parser):
    nested = 'bash -c ' + shlex.quote("echo \"$(echo 'a\"b')\"; " + SUFFIX)
    command = nested + "\ntee fixture <<'EOF'\nx<`\nEOF\n" + SUFFIX
    segments, blind = parser.analyze_checked(command)
    assert blind is not None and segments == []
    assert parser.rewrites_before_running(command)


def test_supported_control_stays_readable(parser):
    segments, blind = parser.analyze_checked('echo fixture; ' + SUFFIX)
    assert blind is None
    assert [segment.argv for segment in segments][-1] == ['composition_probe', 'tail']


def test_size_bound_is_not_reported_as_a_heredoc_rewrite(parser):
    command = "tee fixture <<'EOF'\n" + 'x' * parser.MAX_COMMAND_CHARS + '\nEOF\n' + SUFFIX
    segments, blind = parser.analyze_checked(command)
    assert segments == [] and blind is not None and blind.bounds_induced
    assert not parser.over_nested(command)
    assert not parser.rewrites_before_running(command)


@pytest.mark.parametrize('prefix', ['echo prefix; cat', 'cat 2>&1'])
@pytest.mark.parametrize('body', ["it's data", "it's $(echo data)", "it's `echo data`"])
def test_accepted_complex_heredoc_structural_refusals(parser, prefix, body):
    # Owner-approved residue: valid inert data outside the canonical narrow proof
    # remains explicitly refused, never mistaken for a complete executable parse.
    command = 'echo "$(' + prefix + " <<'EOF'\n" + body + '\nEOF\n)"'
    # Keeping the partial body's final byte preserves the quote; the heredoc
    # boundary, not a synthetic tokenization error, now explains the refusal.
    assert parser.analyze_checked(command) == ([], parser._BLIND_HEREDOC_BODY)


@pytest.mark.parametrize('decoded', [False, True])
def test_independent_structural_child_beats_local_carried_body(parser, decoded):
    child = 'bash -c ' + shlex.quote("echo \"$(echo 'a\"b')\"; " + SUFFIX)
    command = child + "\ntee fixture <<'EOF'\nit's data\nEOF\n" + SUFFIX
    if decoded:
        command = 'bash -c ' + shlex.quote(command)
    result = parser._analyze_bounded(command)
    assert result.structural_incomplete is True
    assert result.heredoc_incomplete is True
    assert result.bound_reason is None
    assert parser.analyze_checked(command) == ([], parser._BLIND_STRUCTURAL)
    assert parser.rewrites_before_running(command)


@pytest.mark.parametrize('prefix', ['echo before; cat', '(cat', "cat <<'SECOND'"])
def test_no_whole_fragment_proof_for_complex_prefix(parser, prefix):
    # An empty hidden-suffix list alone cannot grant tokenization relief.
    text = prefix + " <<'EOF'\nit's data\nEOF\n"
    splits, openers, starts, inert = [], [], [], []
    parser.parse_segments(text, _splits=splits, _openers=openers, _starts=starts)
    parser._heredoc_hidden_suffixes(text, splits, openers, None, True, inert)
    assert inert == []


def test_root_proof_not_inherited_by_decoded_script(parser):
    body = "cat <<'EOF'\nit's $(echo data)\nEOF\n)"
    command = 'bash -c ' + shlex.quote(body)
    result = parser._analyze_bounded(command)
    assert result.structural_incomplete is True
    assert parser.analyze_checked(command)[1] is not None
