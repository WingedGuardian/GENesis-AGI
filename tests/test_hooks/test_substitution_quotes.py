"""Substitution extraction follows bash's quoting, not only single quotes.

``_substitution_spans`` tracked single quotes alone, so an apostrophe inside a
double-quoted word (``"don't"``) or an escaped one (``\\'``, ``$'it\\'s'``) opened a
single-quoted span bash never sees. Every substitution after it was skipped, so a push
or an ``rm`` bash runs reached no guard and no blind spot was reported.

The oracle runs each command under bash with recording stand-ins for git, gh and rm,
in a throwaway directory, so nothing real is pushed or deleted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(HOOKS))
import shell_parse as sp  # noqa: E402

PUSH = "git " + "push --force origin main"
PUSH_ARGV = ("git", "push", "--force", "origin", "main")
RM = "rm -rf " + "~/.genesis"

_SHIM = (
    '#!/bin/sh\n{ printf "%s\\037" "${0##*/}" "$@"; printf "\\036"; } >> "$ORACLE_LOG"\nexit 0\n'
)


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("bash") is None:
        pytest.skip("the oracle needs bash")
    root = tmp_path_factory.mktemp("substitution-quotes")
    (root / "bin").mkdir()
    for name in ("git", "gh", "rm"):
        shim = root / "bin" / name
        shim.write_text(_SHIM, encoding="utf-8")
        shim.chmod(0o755)
    (root / "work").mkdir()
    subprocess.run(["git", "init", "-q", str(root / "repo")], check=True)
    return root


def _ran(command: str, sandbox: Path) -> list[tuple[str, ...]]:
    """The argv of every stand-in bash actually executed for ``command``."""
    log = sandbox / "oracle.log"
    log.write_text("", encoding="utf-8")
    # Two independent layers keep the oracle harmless: HOME is the sandbox, so `~`
    # never names a real directory, and the stand-ins must be what bash resolves.
    path = f"{sandbox / 'bin'}:/usr/bin:/bin"
    for name in ("git", "gh", "rm"):
        assert shutil.which(name, path=path) == str(sandbox / "bin" / name)
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command],
        cwd=sandbox / "work",
        env={
            "PATH": path,
            "HOME": str(sandbox / "work"),
            "ORACLE_LOG": str(log),
            "LC_ALL": "C",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    data = log.read_text(encoding="utf-8")
    return [tuple(rec.split("\x1f")[:-1]) for rec in data.split("\x1e") if rec]


def _guard_exit(guard: str, command: str, sandbox: Path) -> int:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "t",
        "cwd": str(sandbox / "repo"),
    }
    done = subprocess.run(
        [sys.executable, str(HOOKS / f"{guard}.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=sandbox / "repo",
    )
    return done.returncode


def _shows_or_refuses(command: str, wanted: tuple[str, ...]) -> None:
    """The operation is visible in both checked views, or nothing is and a bound says why."""
    segments, blind = sp.analyze_checked(command)
    policy, policy_blind = sp.analyze_policy(command)
    if blind is not None and blind.bounds_induced:
        assert segments == [] and policy == [] and policy_blind is not None
        return
    assert any(tuple(s.argv[: len(wanted)]) == wanted for s in segments), (
        f"bash runs {wanted} and the parse neither shows it nor refuses: {blind}"
    )
    assert any(tuple(p.argv[: len(wanted)]) == wanted for p in policy)


HIDDEN_PUSH = {
    "apostrophe in a dq word before it": 'echo "don\'t" $(' + PUSH + ")",
    "apostrophe in the same dq word": "echo \"it's $(" + PUSH + ')"',
    "apostrophe in an assignment": 'm="it\'s" x=$(' + PUSH + ")",
    "backtick after a dq apostrophe": 'echo "don\'t" `' + PUSH + "`",
    "escaped dq then an apostrophe": 'echo "a\\"\'" $(' + PUSH + ")",
    "escaped sq outside quotes": "echo \\' $(" + PUSH + ")",
    "ansi-c escaped sq": "echo $'it\\'s' $(" + PUSH + ")",
    "locale string apostrophe": 'echo $"don\'t" $(' + PUSH + ")",
    "after a nested shell script": 'bash -c true "it\'s" $(' + PUSH + ")",
    "ansi-c opener is literal inside dq": 'echo "$\'x" $(' + PUSH + ")",
}


@pytest.mark.parametrize("name", sorted(HIDDEN_PUSH))
def test_a_push_after_a_misread_apostrophe_is_seen(name: str, sandbox: Path) -> None:
    command = HIDDEN_PUSH[name]
    assert any(a[: len(PUSH_ARGV)] == PUSH_ARGV for a in _ran(command, sandbox)), (
        "oracle: bash must run the push"
    )
    _shows_or_refuses(command, ("git", "push"))
    assert _guard_exit("git_push_guard", command, sandbox) == 2


HIDDEN_RM = {
    "apostrophe in a dq word before it": 'echo "don\'t" $(' + RM + ")",
    "apostrophe in the same dq word": "echo \"it's $(" + RM + ')"',
}


@pytest.mark.parametrize("guard", ["destructive_command_guard", "protected_paths_guard"])
@pytest.mark.parametrize("name", sorted(HIDDEN_RM))
def test_an_rm_after_a_misread_apostrophe_is_refused(name: str, guard: str, sandbox: Path) -> None:
    command = HIDDEN_RM[name]
    assert any(a[:2] == ("rm", "-rf") for a in _ran(command, sandbox)), (
        "oracle: bash must run the rm"
    )
    assert _guard_exit(guard, command, sandbox) == 2


# Single quotes still block extraction: bash runs nothing in these.
NOT_RUN = {
    "plain single quotes": "echo '$(" + PUSH + ")'",
    "dq inside single quotes": "echo '\"' '$(" + PUSH + ")'",
    "single quotes after an escaped dq": 'echo "a\\"" \'$(' + PUSH + ")'",
    "ansi-c body": "echo $'$(" + PUSH + ")'",
}


@pytest.mark.parametrize("name", sorted(NOT_RUN))
def test_a_single_quoted_substitution_is_not_extracted(name: str, sandbox: Path) -> None:
    command = NOT_RUN[name]
    assert not any(a[:2] == ("git", "push") for a in _ran(command, sandbox)), (
        "oracle: bash must not run the push"
    )
    assert not any("push" in body for body in sp._substitutions(command))


def test_bodies_are_cut_where_bash_cuts_them() -> None:
    """Closed bodies, cut where bash cuts them (unterminated ones: see below)."""
    assert sp._substitutions('echo "don\'t" $(a b)') == ["a b"]
    assert sp._substitutions('echo "x $(a) y" `b`') == ["a", "b"]
    assert sp._substitutions("echo $'it\\'s' $(a)") == ["a"]


def test_extraction_never_shrinks_below_the_legacy_scan() -> None:
    """The bash reading only adds bodies. The legacy scan's over-reads stay (``\\$(a)``
    and ``"it's" '$(a)'`` are still extracted), so a misread escape can never hide a
    body the legacy scan found."""
    for text in (
        "echo \\$(a) $(b)",
        'echo "\\$(a)" $(b)',
        "echo \"it's\" '$(a)'",
        "echo \"it's $(a)\" '$(b)'",
    ):
        legacy = set(sp._legacy_substitution_spans(text))
        assert legacy, text
        assert legacy <= set(sp._substitution_spans(text)), text


def test_past_the_added_work_budget_the_parse_fails_closed(sandbox: Path) -> None:
    """Only the legacy scan's misread hides these bodies, so all their work is added
    work; four nested levels that are most of the command exceed two passes over it,
    and the parse reports the over-nested bound rather than descending."""
    command = 'echo "\'" $($($($(' + PUSH + " " + "y" * 400 + "))))"
    assert any(a[: len(PUSH_ARGV)] == PUSH_ARGV for a in _ran(command, sandbox))
    segments, blind = sp.analyze_checked(command)
    assert blind is not None and blind.bounds_induced and segments == []
    assert _guard_exit("git_push_guard", command, sandbox) == 2


def _amplifying(depth: int, fan: int, leaf: str) -> str:
    if depth == 0:
        return leaf
    inner = _amplifying(depth - 1, fan, leaf)
    return " ".join(f'"x\' $({inner})"' for _ in range(fan))


def test_phantom_bodies_cannot_multiply_the_parse_cost() -> None:
    """Each level's phantom bodies differ from its real ones, so without the budget the
    work doubled per level (MEASURED 6.28s against 0.73s on main at 37K characters).
    The guards share a 5s clock across three of them, and a killed guard permits.
    Asserted on the work counted, not the time taken: the parse stops at the budget,
    so the work it does never exceeds the budget."""
    leaf = "true"
    while len(_amplifying(5, 5, leaf + "y")) < sp.MAX_COMMAND_CHARS - 64:
        leaf += "y"
    command = _amplifying(5, 5, leaf)
    unbounded = [0, 10**12]
    sp._analyze_bounded(command, _added_work=unbounded)
    assert unbounded[0] > 2 * len(command), "control: the shape must amplify"
    work = [0, 2 * len(command)]
    result = sp._analyze_bounded(command, _added_work=work)
    assert result.bound_reason == "depth"
    assert 0 < work[0] <= work[1] < unbounded[0], (work, unbounded[0], len(command))


def test_unterminated_and_trailing_forms_do_not_crash() -> None:
    """An unterminated body is cut as the legacy scan cuts it, so the two readings
    agree (bash runs none of an unclosed `$(`), and a lone trailing backslash ends
    the scan."""
    assert sp._quoted_substitution_spans("echo \"it's $(a b") == [(13, 15)]
    assert sp._quoted_substitution_spans("echo `a") == []
    assert sp._quoted_substitution_spans("echo \\") == []


def test_a_message_here_doc_is_not_charged_as_added_work() -> None:
    """The everyday message idiom behind an apostrophe in another flag: the legacy
    scan never reached the body, and its over-read of the backticks in the message
    must not be charged (MEASURED 2026-11-10: 45 real commands of this shape were
    refused as over-nested before)."""
    prose = "It's `a` and \"b\", it's `c`.\n" * 400
    command = "tool create --title \"it's done\" --body \"$(cat <<'EOF'\n" + prose + 'EOF\n)"'
    segments, blind = sp.analyze_checked(command)
    assert blind is None or not blind.bounds_induced
    assert segments


def test_legacy_descents_are_never_charged() -> None:
    """A chain the legacy scan reads is parsed as before, at any depth under the bound."""
    segments, blind = sp.analyze_checked("echo $($($($(ls))))")
    assert blind is None and segments


def test_a_short_chain_behind_a_stray_quote_still_parses() -> None:
    """Up to the depth bound, a chain that is a small part of the command fits."""
    segments, blind = sp.analyze_checked('echo "\'" $($($($($(ls)))))')
    assert blind is None and segments


def _added_work(command: str) -> int:
    work = [0, 10**9]
    sp._analyze_bounded(command, _added_work=work)
    return work[0]


def test_everything_beneath_an_added_body_is_charged() -> None:
    """Every level under a body only the new reading finds is added work: 8 + 5 + 2."""
    assert _added_work("echo \"'\" $($($(xy)))") == 15


@pytest.mark.parametrize("levels", range(1, 9))
def test_the_budget_is_exactly_two_passes_of_added_work(levels: int) -> None:
    """Pins the constant: a chain behind a phantom quote is withheld exactly when its
    added work exceeds twice the command, and only then."""
    command = "echo \"it's\" " + "$(echo " * levels + "x" + ")" * levels
    over = _added_work(command) > 2 * len(command)
    deep = levels >= sp.MAX_SUBSTITUTION_DEPTH + 1
    _, blind = sp.analyze_checked(command)
    assert (blind is not None and blind.bounds_induced) == (over or deep)

