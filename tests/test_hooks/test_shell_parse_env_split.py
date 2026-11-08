"""GNU env is the argv oracle; the only carried program is an owned reporter."""

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]
ENV = shutil.which("gnuenv") or shutil.which("env")
MARKER = "GENESIS_ENV_ARGV:"


@pytest.fixture
def parser():
    return private_module("env_split_parser", ROOT / "scripts/hooks/shell_parse.py")


@pytest.fixture
def shim(tmp_path):
    program = tmp_path / "git"
    program.write_text(
        f"#!{sys.executable}\nimport json,sys\nprint({MARKER!r}+json.dumps(sys.argv[1:]))\n"
    )
    program.chmod(0o755)
    return program


def invoke(argv):
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                               env={"PATH": "/usr/bin:/bin", "R5_VALUE": "synthetic value"})
    records = [json.loads(line[len(MARKER):]) for line in completed.stdout.splitlines()
               if line.startswith(MARKER)]
    return completed.returncode, records


def project(parser, command, expected):
    plain = parser.analyze(command)
    checked, blind = parser.analyze_checked(command)
    policy, policy_blind = parser.analyze_policy(command)
    assert blind is None and policy_blind is None
    assert [(s.exe, s.argv) for s in plain] == [(s.exe, s.argv) for s in checked]
    assert [s.argv for s in plain if s.exe == "git"] == [expected]
    assert [s.argv for s in policy if s.exe == "git"] == [expected]
    assert [s.raw for s in plain if s.depth == 0] == parser.split_segments(command)
    assert policy[0].position == (0,)
    assert policy[0].raw == plain[0].raw


@pytest.mark.parametrize("form", ["-S", "-Sattached", "--split-string", "--split-string=", "-iS", "-iSattached", "-viS"])
def test_native_supported_forms_agree_in_all_three_apis(parser, shim, form):
    body = f"{shlex.quote(str(shim))} push"
    if form.endswith("attached"):
        argv = [ENV, form.removesuffix("attached") + body, "suffix"]
    elif form.endswith("="):
        argv = [ENV, form + body, "suffix"]
    else:
        argv = [ENV, form, body, "suffix"]
    status, records = invoke(argv)
    assert (status, records) == (0, [["push", "suffix"]])
    project(parser, shlex.join(argv), [str(shim), "push", "suffix"])


@pytest.mark.parametrize("tail,expected", [
    (r'push "two words"', ["push", "two words"]),
    (r"push 'two words'", ["push", "two words"]),
    (r'push ""', ["push", ""]),
    (r'push a\fb\nc\rd\te\vf', ["push", "a\fb\nc\rd\te\vf"]),
    (r'push \#hash \$value \"q\" \'q\' \\', ["push", "#hash", "$value", '"q"', "'q'", "\\"]),
    (r'push a\_b "c\_d"', ["push", "a", "b", "c d"]),
    (r"push 'a\nb' 'c\'d' 'e\\f'", ["push", r"a\nb", "c'd", "e\\f"]),
    (r'push a#b # discarded', ["push", "a#b"]),
    (r'push "#kept" \#also', ["push", "#kept", "#also"]),
    (r'push ""#kept a\_# discarded', ["push", "#kept", "a"]),
    (r"push '\c' '${R5_VALUE}'", ["push", r"\c", "${R5_VALUE}"]),
    ("push non\u00a0breaking", ["push", "non\u00a0breaking"]),
    (r'push ab\c discarded', ["push", "ab"]),
    ("push\tspace\nline\vvertical\fform\rcarriage", ["push", "space", "line", "vertical", "form", "carriage"]),
    (r'push ; > output | git status', ["push", ";", ">", "output", "|", "git", "status"]),
])
def test_native_split_grammar_is_argv_not_shell_source(parser, shim, tail, expected):
    argv = [ENV, "-S", f"{shlex.quote(str(shim))} {tail}", "suffix"]
    assert invoke(argv) == (0, [expected + ["suffix"]])
    project(parser, shlex.join(argv), [str(shim), *expected, "suffix"])


@pytest.mark.parametrize("body", ["'unterminated", '"unterminated', 'git push \\', r'\q git push', '"bad\\c" git push', 'git push $R5_VALUE', 'git push ${R5_VALUE}'])
def test_unresolved_or_invalid_split_never_invents_execution(parser, shim, body):
    body = body.replace("git", shlex.quote(str(shim)))
    argv = [ENV, "-S", body]
    status, records = invoke(argv)
    if "${" in body:
        assert (status, records) == (0, [["push", "synthetic value"]])
    else:
        assert status != 0 and records == []
    command = shlex.join(argv)
    segments, blind = parser.analyze_checked(command)
    assert segments == [] and blind is not None and blind.bounds_induced
    assert parser.analyze_policy(command) == ([], blind)
    assert not any(s.exe == "git" for s in parser.analyze(command))


@pytest.mark.parametrize("prefix", [["A=1"], ["--"], ["A=1", "--"], ["-"]])
def test_split_option_after_operand_boundary_is_not_a_carrier(parser, shim, prefix):
    argv = [ENV, *prefix, "-S", f"{shim} push"]
    status, records = invoke(argv)
    assert status != 0 and records == []
    assert not any(s.exe == "git" for s in parser.analyze(shlex.join(argv)))


@pytest.mark.parametrize("body", [
    "-u REMOVE {shim} push", "-- {shim} push", "A=1 {shim} push",
    "-S '{shim} push'", "env -S '{shim} push'", "# ignored",
])
def test_decoded_fields_reenter_env_and_keep_suffix(parser, shim, body):
    suffix = [str(shim), "push"] if body.startswith("#") else ["suffix"]
    argv = [ENV, "-S", body.format(shim=shim), *suffix]
    expected = ["push"] if body.startswith("#") else ["push", "suffix"]
    assert invoke(argv) == (0, [expected])
    project(parser, shlex.join(argv), [str(shim), *expected])


def test_option_value_named_split_flag_is_inert(parser, shim):
    argv = [ENV, "-u", "-S", str(shim), "push"]
    assert invoke(argv) == (0, [["push"]])
    project(parser, shlex.join(argv), [str(shim), "push"])


def test_policy_keeps_real_source_position_context_and_override(parser, shim, tmp_path):
    script = shlex.join([str(shim), "push"])
    body = "A=1 bash -c " + shlex.quote(script)
    trace = tmp_path / "trace"
    command = shlex.join([ENV, "-C", str(tmp_path), "-S", body]) + f" 2>{shlex.quote(str(trace))} # review-override"
    assert invoke(["/bin/bash", "-c", command]) == (0, [["push"]])
    segments, blind = parser.analyze_policy(command)
    assert blind is None
    parent, child = segments
    context = (("chdir", str(tmp_path)), ("assignment", "A=1"))
    assert parent.position == (0,) and parent.contexts == context
    assert parent.targets == ((">", str(trace)),)
    assert child.ancestors == context and child.scope_kind == "shell"
    assert child.position[:2] == (0, 0) and child.depth == 1
    assert child.argv == [str(shim), "push"]
    assert all(s.override for s in parser.analyze(command))
    assert [s.raw for s in parser.analyze(command) if s.depth == 0] == parser.split_segments(command)


def test_literal_env_mention_is_not_a_carrier(parser, shim):
    argv = ["/usr/bin/printf", "%s", f"env -S '{shim} push'"]
    assert invoke(argv)[1] == []
    assert [s.exe for s in parser.analyze(shlex.join(argv))] == ["printf"]


@pytest.mark.parametrize("prefix,suffix", [("A=1 ", ""), ("timeout 5 ", ""), ("if true; then ", "; fi")])
def test_carrier_in_wrapped_shell_command_position(parser, shim, prefix, suffix):
    command = prefix + shlex.join([ENV, "-S", f"{shim} push"]) + suffix
    assert invoke(["/bin/bash", "-c", command]) == (0, [["push"]])
    project(parser, command, [str(shim), "push"])


def test_nested_env_carriers_share_the_existing_depth_budget(parser, shim):
    argv = [str(shim), "push"]
    for _ in range(parser.MAX_SUBSTITUTION_DEPTH + 1):
        argv = [ENV, "-S", shlex.join(argv)]
    assert invoke(argv) == (0, [["push"]])
    command = shlex.join(argv)
    segments, blind = parser.analyze_checked(command)
    assert segments == [] and blind is parser._BLIND_OVER_NESTED
    assert parser.analyze_policy(command) == ([], blind)


@pytest.mark.parametrize("prefix", [["-uS"], ["-iuS"], ["--unset=-S"], ["--", "A=1"], ["-"], []])
def test_option_arity_and_empty_split_suffix(parser, shim, prefix):
    argv = [ENV, *prefix, str(shim), "push", "-S", "git status"]
    assert invoke(argv) == (0, [["push", "-S", "git status"]])
    project(parser, shlex.join(argv), [str(shim), "push", "-S", "git status"])
    empty = [ENV, "--split-string=", str(shim), "push"]
    assert invoke(empty) == (0, [["push"]])
    project(parser, shlex.join(empty), [str(shim), "push"])


@pytest.mark.parametrize("prefix", [["-zS"], ["--not-an-option"], ["--version"], ["-0"], ["-i0S"]])
def test_unproven_option_boundary_does_not_invent_carried_execution(parser, shim, prefix):
    argv = [ENV, *prefix, "-S", f"{shim} push"]
    assert invoke(argv)[1] == []
    command = shlex.join(argv)
    assert not any(s.exe == "git" for s in parser.analyze(command))
    segments, blind = parser.analyze_checked(command)
    assert segments == [] and blind is not None and blind.bounds_induced
    assert parser.analyze_policy(command) == ([], blind)


@pytest.mark.parametrize("shells,carriers", [(0, 5), (1, 4), (2, 4)])
def test_shell_and_env_share_one_bound_without_changing_source_depth(parser, shim, shells, carriers):
    argv = [str(shim), "push"]
    for _ in range(carriers):
        argv = [ENV, "-S", shlex.join(argv)]
    for _ in range(shells):
        argv = ["/bin/bash", "-c", shlex.join(argv)]
    assert invoke(argv) == (0, [["push"]])
    command = shlex.join(argv)
    segments, blind = parser.analyze_checked(command)
    policy, policy_blind = parser.analyze_policy(command)
    if shells + carriers > parser.MAX_SUBSTITUTION_DEPTH:
        assert segments == policy == [] and blind is policy_blind is parser._BLIND_OVER_NESTED
    else:
        assert blind is policy_blind is None
        assert [(s.argv, s.depth) for s in segments if s.exe == "git"] == [([str(shim), "push"], shells)]
        assert [(s.argv, s.depth) for s in policy if s.exe == "git"] == [([str(shim), "push"], shells)]


def test_carrier_preserves_source_groups_and_separate_substitution_context(parser, shim, tmp_path):
    first = shlex.join([ENV, "-C", str(tmp_path), "-S", f"{shim} push"])
    command = first + "; " + shlex.join([ENV, "-C", str(tmp_path), "-S", "printf %s"]) + ' "$(' + str(shim) + ' status)"'
    status, records = invoke(["/bin/bash", "-c", command])
    assert status == 0 and records == [["push"], ["status"]]
    policy, blind = parser.analyze_policy(command)
    assert blind is None
    # Policy positions mark the source range after ';', including its leading space.
    assert [s.position for s in policy if not s.depth] == [(0,), (len(first) + 1,)]
    child = [s for s in policy if s.depth][0]
    assert child.scope_kind == "substitution" and child.ancestors == ()
    plain = parser.analyze(command)
    assert [s.raw for s in plain if s.depth == 0] == parser.split_segments(command)
    assert [(s.exe, s.argv) for s in plain] == [(s.exe, s.argv) for s in parser.analyze_checked(command)[0]]


def test_env_input_length_cap_still_withholds_partial_records(parser):
    command = "env -S 'git push " + "x" * parser.MAX_COMMAND_CHARS + "'"
    assert parser.analyze_checked(command) == ([], parser._BLIND_OVER_LONG)
    assert parser.analyze_policy(command) == ([], parser._BLIND_OVER_LONG)


@pytest.mark.parametrize("form", ["plain", "-S", "--split-string=", "-iS", "inert"])
def test_actual_push_guard_consumes_the_carried_operation(parser, shim, tmp_path, form):
    operation = [str(shim), "push", "--force", "origin", "main"]
    body = shlex.join(operation)
    argv = operation if form == "plain" else [ENV, form, body]
    if form.endswith("="):
        argv = [ENV, form + body]
    elif form == "inert":
        argv = ["/usr/bin/printf", "%s", body]
    status, records = invoke(argv)
    assert status == 0
    assert records == ([] if form == "inert" else [operation[1:]])
    command = shlex.join(argv)
    if form != "inert":
        project(parser, command, operation)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/hooks/git_push_guard.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        text=True, capture_output=True, timeout=15, cwd=tmp_path,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path),
             "GENESIS_HOME": str(tmp_path / "genesis-home")},
    )
    assert result.returncode == (0 if form == "inert" else 2), result.stderr
    if form != "inert":
        assert "Force push" in result.stderr
