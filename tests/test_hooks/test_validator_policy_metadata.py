"""The opt-in policy view must retain facts the historical view omits."""
import inspect
from dataclasses import asdict
from pathlib import Path

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def parser():
    path = ROOT / 'scripts/hooks/shell_parse.py'
    module = private_module('validator_policy_parser', path)
    assert Path(module.__file__).resolve() == path.resolve()
    return module


def policy(parser, command):
    # The old public view is the unfenced control before this new API exists.
    return getattr(parser, 'analyze_policy', parser.analyze_checked)(command)


@pytest.mark.parametrize(('command', 'expected'), [
    ('echo value > /synthetic/data/store', [('>', '/synthetic/data/store')]),
    ('echo value >> /synthetic/data/store', [('>>', '/synthetic/data/store')]),
    ('echo value >| /synthetic/data/store', [('>|', '/synthetic/data/store')]),
    ('echo value 2>/synthetic/data/store', [('>', '/synthetic/data/store')]),
    ('echo value &>/synthetic/data/store', [('&>', '/synthetic/data/store')]),
    ('echo value &>>/synthetic/data/store', [('&>>', '/synthetic/data/store')]),
    ('echo value 2>&1', [('>&', '1')]),
    ('cat < /synthetic/input', [('<', '/synthetic/input')]),
    ('true <> /synthetic/data/store', [('<', ''), ('>', '/synthetic/data/store')]),
    ('echo value > "/synthetic/path with spaces/λ"', [('>', '"/synthetic/path with spaces/λ"')]),
])
def test_policy_retains_literal_redirect_operators_and_raw_targets(parser, command, expected):
    segments, blind = policy(parser, command)
    assert blind is None
    assert [target for segment in segments for target in getattr(segment, 'targets', ())] == expected


@pytest.mark.parametrize('command', ['> /synthetic/data/store', '2> /synthetic/data/store'])
def test_redirect_only_occurrence_survives_only_in_opt_in_view(parser, command):
    assert parser.analyze_checked(command) == ([], None)
    segments, blind = policy(parser, command)
    assert blind is None and len(segments) == 1
    assert segments[0].exe == '' and segments[0].argv == []
    assert segments[0].targets == (('>', '/synthetic/data/store'),)
    assert segments[0].position == (0,)


def test_positions_are_original_occurrences_not_rewritten_raw_matches(parser):
    command = '> first.log git status; cd /synthetic/main; git status > second.log'
    segments, blind = policy(parser, command)
    assert blind is None
    assert [getattr(s, 'position', None) for s in segments] == [(0,), (23,), (43,)]
    assert [s.exe for s in segments] == ['git', 'cd', 'git']
    assert segments[0].targets == (('>', 'first.log'),)
    assert segments[-1].targets == (('>', 'second.log'),)


@pytest.mark.parametrize(('command', 'contexts'), [
    ('env -C /synthetic/scratch git status', (('chdir', '/synthetic/scratch'),)),
    ('env --chdir=/synthetic/scratch git status', (('chdir', '/synthetic/scratch'),)),
    ('env -C/synthetic/scratch git status', (('chdir', '/synthetic/scratch'),)),
    ('env -u -C git status', ()),
    ('env --unset=-C git status', ()),
    ('env -iC/synthetic/scratch git status', (('unknown', None),)),
    ('sudo -n git status', (('opaque', None),)),
    ('timeout 5 git status', ()),
])
def test_context_is_captured_at_actual_wrapper_option_bindings(parser, command, contexts):
    segments, blind = policy(parser, command)
    assert blind is None and segments[0].exe == 'git'
    assert getattr(segments[0], 'contexts', ()) == contexts


@pytest.mark.parametrize(('command', 'ancestors'), [
    ('env -C /synthetic/scratch bash -c "git status"', (('chdir', '/synthetic/scratch'),)),
    ('env -C /synthetic/scratch echo "$(git status)"', ()),
    ('sudo -n bash -c "git status"', (('opaque', None),)),
    ('sudo -n echo "$(git status)"', ()),
])
def test_shell_body_and_argument_substitution_keep_distinct_contexts(parser, command, ancestors):
    segments, blind = policy(parser, command)
    assert blind is None
    nested = [s for s in segments if s.depth > 0 and s.exe == 'git']
    assert len(nested) == 1
    assert getattr(nested[0], 'ancestors', None) == ancestors


@pytest.mark.parametrize('command', [
    'echo value', 'git status', 'git 2>/dev/null status', '> /synthetic/output',
    'env -C /synthetic/scratch git status', 'bash -c "git status"',
    'echo "$(git status)"', 'echo "> /synthetic/data/store"',
])
def test_default_segment_shape_and_values_are_unchanged_by_policy_scan(parser, command):
    original, blind = parser.analyze_checked(command)
    expected = [asdict(s) for s in original]
    policy(parser, command)
    repeated, repeated_blind = parser.analyze_checked(command)
    assert blind == repeated_blind and expected == [asdict(s) for s in repeated]
    for segment in repeated:
        assert type(segment) is parser.Segment
        assert set(asdict(segment)) == {'exe', 'argv', 'override', 'raw', 'depth', 'redirects', 'verb_unresolved'}


@pytest.mark.parametrize('case', ['length', 'depth', 'verb', 'continuation'])
def test_blind_policy_view_withholds_every_partial_record(parser, case):
    command = {'length': 'x' * 65537, 'verb': 'git $operation',
               'continuation': 'echo value ' + chr(92) + '\n' + 'git push'}.get(case)
    if case == 'depth':
        command = 'git push'
        for _ in range(parser.MAX_SUBSTITUTION_DEPTH + 2):
            command = 'echo $(' + command + ')'
    _, expected_blind = parser.analyze_checked(command)
    assert expected_blind is not None
    segments, blind = policy(parser, command)
    assert blind == expected_blind and segments == []


@pytest.mark.parametrize(('argv', 'expected'), [
    (['git', '-C', '/synthetic/scratch', 'commit'], [('-C', '/synthetic/scratch')]),
    (['git', '-C/synthetic/scratch', 'commit'], [('-C', '/synthetic/scratch')]),
    (['git', '-c', 'fixture=-C/synthetic/main', 'commit'], [('-c', 'fixture=-C/synthetic/main')]),
    (['git', '--namespace', '-C/synthetic/main', 'commit'], [('--namespace', '-C/synthetic/main')]),
    (['git', '--git-dir=/synthetic/main/.git', 'commit'], [('--git-dir', '/synthetic/main/.git')]),
    (['git', '-C', '/synthetic/one', '-C', 'two', 'commit'], [('-C', '/synthetic/one'), ('-C', 'two')]),
    (['git', '--no-pager', 'commit', '-C', '/synthetic/main'], [('--no-pager', None)]),
])
def test_optional_git_bindings_come_from_the_canonical_global_walk(parser, argv, expected):
    options = []
    original = parser.git_subcommand_index(argv)
    if '_options' in inspect.signature(parser.git_subcommand_index).parameters:
        repeated = parser.git_subcommand_index(argv, _options=options)
    else:
        repeated = original  # Old-source control: no facts, no import/arity error.
    assert repeated == original == argv.index('commit')
    assert options == expected


@pytest.mark.parametrize(('command', 'assignment'), [
    ('GIT_DIR=/synthetic/main/.git bash -c "git status"', 'GIT_DIR=/synthetic/main/.git'),
    ('env GIT_DIR=/synthetic/main/.git bash -c "git status"', 'GIT_DIR=/synthetic/main/.git'),
    ('env -- HOME=/synthetic/home bash -c "git status"', 'HOME=/synthetic/home'),
])
def test_consumed_assignments_reach_executable_shell_bodies(parser, command, assignment):
    segments, blind = policy(parser, command)
    assert blind is None
    assert ('assignment', assignment) in segments[0].contexts
    assert ('assignment', assignment) in segments[1].ancestors


@pytest.mark.parametrize('prefix', ['GIT_DIR=/synthetic/main/.git', 'env GIT_DIR=/synthetic/main/.git'])
def test_new_assignments_do_not_reach_argument_substitutions(parser, prefix):
    segments, blind = policy(parser, prefix + ' echo "$(git status)"')
    assert blind is None
    assert segments[0].contexts == (('assignment', 'GIT_DIR=/synthetic/main/.git'),)
    assert segments[1].ancestors == ()


@pytest.mark.parametrize(('command', 'kind'), [
    ('bash -c "git status"', 'shell'),
    ('echo "$(git status)"', 'substitution'),
    ('case fixture in fixture) cd /synthetic/main;; esac', 'embedded'),
    ('fixture() { cd /synthetic/main; }', 'embedded'),
])
def test_canonical_origin_distinguishes_real_child_scopes_from_embedded_bodies(parser, command, kind):
    segments, blind = policy(parser, command)
    assert blind is None
    assert getattr(segments[0], 'scope_kind', None) == 'root'
    nested = [s for s in segments if s.depth]
    assert nested and all(getattr(s, 'scope_kind', None) == kind for s in nested)
