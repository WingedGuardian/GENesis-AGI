"""Substitution boundaries must not borrow their enclosing word's quote state."""

import shlex

import pytest

from tests.test_hooks.test_nested_structural_refusal_class import (
    _DISCARD,
    _PUSH_GUARD,
    _WORKTREE,
    _refused,
    _run,
    sandbox,
)
from tests.test_hooks.test_unquoted_escape_boundaries import owned_oracle, sp

__all__ = ["owned_oracle", "sandbox"]


@pytest.mark.parametrize("body", [
    '''ls "$(cat 'q"r')"; curl __genesis_oracle_2__; ls "$(cat 'q"r')"''',
    '''ls "it's $(curl __genesis_oracle_2__)"''',
    r"ls a\'b$(curl __genesis_oracle_2__)",
    '''ls "$(cat "$(curl __genesis_oracle_2__)" 'q"r')"''',
    '''ls >"$(cat 'q"r')out"; curl __genesis_oracle_2__''',
    r"echo `echo \`curl __genesis_oracle_2__\``",
    r'echo "`echo \`curl __genesis_oracle_2__\``"',
    r"echo `echo \$(curl __genesis_oracle_2__)`",
    r"echo $(echo `echo \`curl __genesis_oracle_2__\``)",
    r"echo `echo \`curl __genesis_oracle_2__\`; echo done`",
    r"echo `echo \$(curl __genesis_oracle_2__); echo done`",
    '''ls "`cat 'q"r'`"; curl __genesis_oracle_2__; ls "`cat 'q"r'`"''',
    "echo $(echo `curl __genesis_oracle_2__`; echo done)",
])
@pytest.mark.parametrize("nested", [False, True])
def test_executed_occurrence_is_never_silently_lost(owned_oracle, body, nested):
    command = "bash -c " + shlex.quote(body) if nested else body
    assert ("curl", "__genesis_oracle_2__") in owned_oracle.run(command)
    segments, blind = sp.analyze_checked(command)
    assert blind is not None or any(
        s.argv == ["curl", "__genesis_oracle_2__"] for s in segments
    )


@pytest.mark.parametrize("command", [
    r"ls \`curl\`",
    r'''ls "\`curl\`"''',
    r"ls \$(curl)",
    r"ls `curl\`",
])
def test_escaped_openers_are_not_extracted(command):
    assert sp._substitution_spans(command) == []


def test_paired_substitutions_keep_raw_source_and_suffix(owned_oracle):
    command = '''ls "$(cat 'q"r')"; curl __genesis_oracle_2__; ls "$(cat 'q"r')"'''
    assert ("curl", "__genesis_oracle_2__") in owned_oracle.run(command)
    parts = sp.parse_segments(command)
    assert [p.raw for p in parts] == [part.strip() for part in command.split(";")]
    assert sp._substitutions(parts[0].raw) == ['cat \'q"r\'']


@pytest.mark.parametrize("command", [
    '''ls "it's $(curl __genesis_oracle_2__)"''',
    r"ls a\'b$(curl __genesis_oracle_2__)",
])
def test_supported_substitution_contexts_remain_readable(command):
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert any(s.argv == ["curl", "__genesis_oracle_2__"] and s.depth == 1 for s in segments)


def test_shlex_retained_escape_is_not_trusted_as_a_literal(owned_oracle):
    command = r'''bash -c "ls \"\$(curl __genesis_oracle_2__)\""'''
    assert ("curl", "__genesis_oracle_2__") in owned_oracle.run(command)
    assert sp.analyze_checked(command) == ([], sp._BLIND_STRUCTURAL)


@pytest.mark.parametrize("program", ["git", "gh"])
@pytest.mark.parametrize("tail", ["", "; echo done"])
def test_backtick_decoding_keeps_refusal_only_recovery(program, tail):
    command = r"echo `echo \`PROGRAM $ACTION\`TAIL`".replace("PROGRAM", program).replace("TAIL", tail)
    assert sp.analyze_checked(command) == ([], sp._BLIND_STRUCTURAL)
    assert sp.unresolved_verb_programs(command) == frozenset({program})


@pytest.mark.parametrize("program", ["git", "gh"])
@pytest.mark.parametrize("separator", [";", "\n", "&&", "||", "|", "|&", "&"])
def test_unquoted_container_preserves_unresolved_dispatcher(program, separator):
    command = f"echo $(echo `{program} $ACTION` {separator} echo done)"
    assert sp.analyze_checked(command)[1] is not None
    assert sp.unresolved_verb_programs(command) == frozenset({program})


@pytest.mark.parametrize("operator", ["|", "&", "||", "&&"])
@pytest.mark.parametrize("substitution", ["`OP`", "$(OP)"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("side", ["left", "right"])
def test_arithmetic_fragment_never_loses_an_operation_or_argument(
    owned_oracle, operator, substitution, nested, side,
):
    operands = [f"0{substitution}", "1"] if side == "left" else ["1", f"0{substitution}"]
    body = ("echo $((" + f" {operator} ".join(operands) + "))").replace(
        "OP", "curl __genesis_oracle_2__",
    )
    command = "bash -c " + shlex.quote(body) if nested else body
    assert ("curl", "__genesis_oracle_2__") in owned_oracle.run(command)
    assert sp.analyze_checked(command) == ([], sp._BLIND_STRUCTURAL)
    assert any(s.argv == ["curl", "__genesis_oracle_2__"] for s in sp.analyze(command))


@pytest.mark.parametrize("word", ["$(echo", "$(echo $(date)", "$(echo `date`", "$(echo ')'"])
def test_unmatched_boundary_is_not_a_closing_delimiter(word):
    assert sp._command_sub_end(word, 0, len(word)) is None
    assert sp._word_left_open(word)
    assert sp._substitutions(word)[0] == word[2:]
    assert sp.analyze_checked("echo " + word) == ([], sp._BLIND_STRUCTURAL)


@pytest.mark.parametrize("program", ["git", "gh"])
@pytest.mark.parametrize("substitution", ["`OP`", "$(OP)"])
def test_arithmetic_incompleteness_keeps_refusal_only_recovery(program, substitution):
    command = f"echo $((0{substitution} | 1))".replace("OP", f"{program} $ACTION")
    assert sp.analyze_checked(command) == ([], sp._BLIND_STRUCTURAL)
    assert sp.unresolved_verb_programs(command) == frozenset({program})


@pytest.mark.parametrize("nested", [False, True])
def test_proven_body_does_not_prove_an_unclosed_outer_boundary(owned_oracle, nested):
    body = '''echo "$(cat <<'EOF'
the container's key
EOF
)"; curl __genesis_oracle_2__'''
    command = "bash -c " + shlex.quote(body) if nested else body
    assert ("curl", "__genesis_oracle_2__") in owned_oracle.run(command)
    assert sp.analyze_checked(command) == ([], sp._BLIND_STRUCTURAL)


def _mixed_heredocs(prefix="echo prefix; cat", body="it's data"):
    return f'''echo "$({prefix} <<'EOF'
{body}
EOF
)"
cat <<'END'
it's data
END'''


@pytest.mark.parametrize("prefix", ["echo prefix; cat", "cat 2>&1"])
@pytest.mark.parametrize("body", ["it's data", "it's $(echo data)", "it's `echo data`"])
def test_mixed_diagnostics_keep_public_withholding_contract(prefix, body):
    command = "git $ACTION; " + _mixed_heredocs(prefix, body)
    result = sp._analyze_bounded(command)
    assert result.structural_incomplete and sp._raw_untokenizable(command)
    assert result.diagnostic is not None and result.diagnostic.bounds_induced
    assert sp.analyze_checked(command) == ([], result.diagnostic)
    assert sp.unresolved_verb_programs(command) == frozenset({"git"})


def test_nonstructural_untokenizable_keeps_available_segments():
    command = "git status; echo 'unfinished"
    assert not sp._analyze_bounded(command).structural_incomplete
    segments, blind = sp.analyze_checked(command)
    assert blind is sp._BLIND_UNTOKENIZABLE and not blind.bounds_induced
    assert any(s.argv == ["git", "status"] for s in segments)


@pytest.mark.parametrize("guard,operation", [
    (_PUSH_GUARD, "git $ACTION"),
    (_WORKTREE / "scripts/hooks/protected_paths_guard.py", "rm -rf ~/genesis"),
    (_WORKTREE / "scripts/hooks/full_suite_guard.py", "pytest"),
])
def test_mixed_diagnostics_preserve_real_guard_refusals(sandbox, guard, operation):
    cwd, env = sandbox
    for command in (operation, operation + "; " + _mixed_heredocs()):
        result = _run(guard, command, cwd, env)
        assert _refused(result), (command, result.returncode, result.stdout, result.stderr)


@pytest.mark.parametrize("guard,operation", [
    (_PUSH_GUARD, "git push --force origin main"),
    (_DISCARD, "git clean -fdx"),
    (_WORKTREE / "scripts/review_enforcement_commit.py", "git commit --no-verify -m fixture"),
])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("template", [
    '''echo "$(echo 'q"r')"; OP; echo "$(echo 'q"r')"''',
    r"echo `echo \`OP\``",
    r"echo `echo \$(OP)`",
    r"echo `echo \`OP\`; echo done`",
    r"echo `echo \$(OP); echo done`",
    '''echo "`echo 'q"r'`"; OP; echo "`echo 'q"r'`"''',
    "echo $(echo `OP`; echo done)",
    "echo $((0`OP` | 1))",
    "echo $((0$(OP) | 1))",
])
def test_real_guards_refuse_hidden_operations(sandbox, guard, operation, nested, template):
    cwd, env = sandbox
    body = template.replace("OP", operation)
    command = "bash -c " + shlex.quote(body) if nested else body
    for text in (operation, command):
        result = _run(guard, text, cwd, env)
        assert _refused(result), (text, result.returncode, result.stdout, result.stderr)
