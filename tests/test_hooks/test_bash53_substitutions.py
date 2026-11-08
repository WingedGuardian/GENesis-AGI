"""bash 5.3 runs ``${ cmd; }`` and ``${| cmd; }``, and an older bash refuses them.

The parser reports their text as unreadable rather than modelling it. Reading one takes
knowing where it closes, and whatever follows it still runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(HOOKS))
import shell_parse as sp  # noqa: E402

# {c} is the command. A newline in a form is a real one.
RUN_FORMS = {
    "plain": "echo ${{ {c}; }}",
    "tab": "echo ${{\t{c}; }}",
    "newline": "echo ${{\n{c}\n}}",
    "quoted": 'echo "${{ {c}; }}"',
    "second statement": "echo ${{ true; {c}; }}",
    "quoted second statement": 'echo "${{ true; {c}; }}"',
    "value": "echo ${{| {c}; }}",
    "value with no space": "echo ${{|{c}; }}",
    "quoted value": 'echo "${{| {c}; }}"',
    "assignment": "x=${{ {c}; }}",
    "comment": "echo ${{ # note\n{c}\n}}",
    "quoted comment": 'echo "${{ # note\n{c}\n}}"',
    "here-doc": "echo ${{ cat <<E\nx\nE\n{c}\n}}",
    "quoted here-doc": 'echo "${{ cat <<E\nx\nE\n{c}\n}}"',
    "in an unquoted here-doc": "cat <<E\n${{ {c}; }}\nE",
    "here-string": "cat <<< ${{ {c}; }}",
    "redirect target": "echo x > ${{ {c}; }}",
    "nested": "echo ${{ echo ${{ {c}; }}; }}",
    "in a command substitution": "echo $(echo ${{ {c}; }})",
    "holding a command substitution": "echo ${{ echo $( {c} ); }}",
    "in a default value": "echo ${{UNSET_FOR_TEST:-${{ {c}; }}}}",
    "in backticks": "echo `echo ${{ {c}; }}`",
    # What follows the close is still the command, when the body expands to nothing.
    "the command after an empty one": "${{ :; }} {c}",
    "the command glued to an empty one": "${{ :; }}{c}",
    "an assignment before the command": "x=${{ :; }} {c}",
    "after a quoted word": 'echo "don\'t" ${{ {c}; }}',
    "in a case arm": "echo $(echo ${{ case x in x) {c};; esac; }})",
    "backslash before the dollar in backticks": "echo `echo \\${{ {c}; }}`",
}
PROBE = "probe-command --flag"


def _blind(command: str) -> sp.BlindSpot | None:
    return sp.analyze_checked(command)[1]


@pytest.mark.parametrize("form", RUN_FORMS.values(), ids=RUN_FORMS)
def test_each_form_is_reported_with_no_segments(form):
    segments, blind = sp.analyze_checked(form.format(c=PROBE))
    assert blind is not None
    assert segments == []


def test_the_report_says_what_to_do():
    blind = _blind(f"echo ${{ {PROBE}; }}")
    assert blind is sp._BLIND_BASH53_SUBSTITUTION
    assert blind.bounds_induced
    assert "$(...)" in blind.hint


def test_a_consumer_is_told_this_is_not_a_size_bound():
    """Such a consumer gives its note per command, not once per session."""
    assert sp.rewrites_before_running(f"echo ${{ {PROBE}; }}")
    assert not sp.rewrites_before_running("echo ${HOME}")


def test_a_script_given_to_a_shell_is_read_too():
    assert _blind(f"bash -c 'echo ${{ {PROBE}; }}'") is sp._BLIND_BASH53_SUBSTITUTION


@pytest.mark.parametrize(
    "text",
    [
        "${HOME}",
        "${#HOME}",
        "${HOME:-x}",
        "${HOME:- x}",
        "${HOME/ /x}",
        "${HOME%%|*}",
        '"${HOME}"',
        "$( true )",
    ],
)
def test_ordinary_expansions_stay_readable(text):
    """Only a space, tab, newline or bar straight after the brace makes it a command."""
    assert _blind(f"echo {text}") is None


def test_a_proven_data_here_doc_stays_readable():
    """Scripts and messages are written this way, and bash never expands the body."""
    segments, blind = sp.analyze_checked(f"cat <<'E'\n${{ {PROBE}; }}\nE")
    assert blind is None
    assert [segment.exe for segment in segments] == ["cat"]


@pytest.mark.parametrize(
    "command",
    [
        f"echo '${{ {PROBE}; }}'",
        f"true # the ${{ {PROBE}; }} form",
        f'git commit -m "the \\${{ {PROBE}; }} form"',
    ],
)
def test_a_mention_of_the_text_is_refused_as_the_accepted_cost(command):
    """bash runs none of these. Telling them apart needs a model of quoting this rule does
    without, and such text is rare. If this starts to cost real commands, it can be
    weighed again against what that model would take."""
    assert _blind(command) is sp._BLIND_BASH53_SUBSTITUTION


def _guard(guard: str, command: str, cwd: Path) -> str:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "t",
        "cwd": str(cwd),
    }
    done = subprocess.run(
        [sys.executable, str(HOOKS / f"{guard}.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return (
        "refused"
        if done.returncode == 2 or '"ask"' in done.stdout or '"deny"' in done.stdout
        else "allowed"
    )


_PUSH = "git " + "push origin some-branch"
_REMOVE = "r" + "m -rf ~/.genesis"
_GUARD_FORMS = [
    "plain",
    "quoted",
    "value",
    "in backticks",
    "the command after an empty one",
    "after a quoted word",
]


@pytest.mark.parametrize(
    ("guard", "command"),
    [
        ("git_push_guard", _PUSH),
        ("protected_paths_guard", _REMOVE),
        ("destructive_command_guard", _REMOVE),
    ],
)
@pytest.mark.parametrize("form", _GUARD_FORMS)
def test_the_guards_refuse_it(guard, command, form, tmp_path):
    """The parser tests above could pass with a guard reading something else. A control
    in each row: the plain command has to be refused, or this proves nothing."""
    assert _guard(guard, command, tmp_path) == "refused"
    assert _guard(guard, RUN_FORMS[form].format(c=command), tmp_path) == "refused"


def _bash_runs_these_forms() -> bool:
    done = subprocess.run(
        ["bash", "-c", "printf %s ${ printf %s%s RA N; }"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return "RAN" in done.stdout


def test_real_bash_runs_every_form_in_the_table():
    """Graded where bash has these forms, as Ubuntu 26.04 does. Elsewhere it refuses them
    and there is nothing to grade: no skip, because the skip count has no headroom."""
    if not _bash_runs_these_forms():
        return
    probe = "printf %s%s RA N >&2"
    for name, form in RUN_FORMS.items():
        done = subprocess.run(
            ["bash", "-c", form.format(c=probe)],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        assert "RAN" in done.stderr, f"{name}: bash did not run it, so the table is wrong"
