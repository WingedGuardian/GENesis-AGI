"""A line continuation is a blind spot the parser REPORTS, not one it models.

THE DEFECT. The shell deletes an unescaped backslash-newline and runs the two lines
as one command; the parser splits there instead. A gated verb on the far side of the
split landed in a segment of its own, the guards found no operation, and "not found"
was read as "not present". MEASURED on the pre-change tree, each guard driven as a
subprocess with the one-line form as the control: four guards gave the continued
command a different verdict from the one-line command.

THE FIX IS A REFUSAL, NOT A READING. Review rounds on attempts that modelled the join
(continuation groups, joined views, heredoc and quote readings, and later a recovery
of the segments from several readings) each found new defects in that modelling. So
a continuation is reported by ``analyze_checked`` as a BOUNDS-type blind spot: the
segments cannot be trusted, and each consumer refuses when its early exit says the
command names its operation, or, for an advisory, gives a short note.

The early exits read ``shell_parse.mentions`` rather than the raw text, so a
continuation or a quote INSIDE the verb (which the shell removes) cannot hide the
verb from the early exit and skip the parse altogether. It searches each reading of
the command on its own: a match assembled across two readings is text the shell
never runs.

Trigger literals are assembled from fragments so this file's own text does not carry
them, per the convention in the other guard suites.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import shell_parse as sp  # noqa: E402

_PY = sys.executable

# ── trigger literals assembled from fragments ──
GIT = "git"
CLEAN = "cl" + "ean"
PUSH = "pu" + "sh"
COMMIT = "com" + "mit"
RM = "r" + "m"
FORCE = "--" + "for" + "ce"
NV = "--no-" + "ver" + "ify"

CONT = " \\\n  "  # a continuation between two words
MID = "\\\n"  # a continuation with nothing around it: splits a WORD

# Every guard gets the same ALLOWLIST environment (see test_guard_ansic_fail_closed
# for the two ambient inputs that once changed verdicts): nothing inherited reaches a
# guard unless it is named here, and HOME is pinned per test to a sandbox.
_NEUTRAL_ENV = ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


def _env(home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _NEUTRAL_ENV if k in os.environ}
    env["HOME"] = str(home)
    env["GENESIS_HOME"] = str(home / ".genesis")
    return env


def _repo(tmp: Path) -> Path:
    """A throwaway repository on a feature branch, outside any other repository."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature-x", str(repo)], check=True)
    return repo


@pytest.fixture
def sandbox():
    # Not tmp_path: the scratch root must sit outside every repository so a guard
    # resolving the cwd's repo can only ever find the one built here.
    with tempfile.TemporaryDirectory(prefix="cont-refusal-") as d:
        root = Path(d)
        home = root / "home"
        (home / "genesis" / "data").mkdir(parents=True)
        yield home, _repo(root)


def _run(script: Path, command: str, home: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_PY, str(script)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_env(home),
        timeout=120,
    )


def _refused(res: subprocess.CompletedProcess) -> bool:
    """Exit 2, or a JSON deny. An ``ask`` is not a refusal and is reported as such."""
    try:
        out = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    decision = out.get("hookSpecificOutput", {}).get("permissionDecision", "")
    return res.returncode == 2 or decision == "deny"


_DISCARD = _HOOKS_DIR / "git_discard_guard.py"
_PUSH_GUARD = _HOOKS_DIR / "git_push_guard.py"
_PROTECTED = _HOOKS_DIR / "protected_paths_guard.py"
_DESTRUCTIVE = _HOOKS_DIR / "destructive_command_guard.py"
_COMMIT_GATE = _WORKTREE / "scripts" / "review_enforcement_commit.py"

# (id, guard, one-line gated command). Each one-line form is REFUSED today; that is
# asserted first in every cell, so a fixture that never reaches its gate fails loudly
# instead of letting the continued cell pass for the wrong reason.
_GATED = [
    ("discard", _DISCARD, f"{GIT} {CLEAN} -fd"),
    ("push", _PUSH_GUARD, f"{GIT} {PUSH} origin main {FORCE}"),
    ("protected", _PROTECTED, f"{RM} -rf ~/genesis/data"),
    ("destructive", _DESTRUCTIVE, f"{RM} -rf ~"),
    ("commit", _COMMIT_GATE, f"{GIT} {COMMIT} {NV} -m x"),
]


def _variants(one_line: str) -> dict[str, str]:
    """Every place a continuation can sit: between words, and inside each of the
    first two words (the program and the verb)."""
    words = one_line.split(" ")
    out = {"between-first-words": one_line.replace(" ", CONT, 1)}
    out["before-last-word"] = " ".join(words[:-1]) + CONT + words[-1]
    first = words[0]
    out["inside-program"] = first[:1] + MID + first[1:] + " " + " ".join(words[1:])
    if len(words) > 1 and len(words[1]) > 1:
        second = words[1]
        out["inside-verb"] = " ".join([first, second[:1] + MID + second[1:], *words[2:]])
    return out


_CELLS = [
    pytest.param(guard, one_line, variant, id=f"{gid}-{name}")
    for gid, guard, one_line in _GATED
    for name, variant in _variants(one_line).items()
]


@pytest.mark.parametrize(("guard", "one_line", "continued"), _CELLS)
def test_a_continued_gated_command_is_refused_like_its_one_line_form(
    sandbox, guard, one_line, continued
):
    """The acceptance bar: the measured bypass, replayed through the real guard."""
    home, repo = sandbox
    plain = _run(guard, one_line, home, repo)
    assert _refused(plain), (
        f"{guard.name} did not refuse the one-line form, so this fixture never reaches "
        f"its gate and the continued cell would prove nothing.\n{plain.stderr[:400]}"
    )
    cont = _run(guard, continued, home, repo)
    assert _refused(cont), (
        f"{guard.name} allowed a continued command it refuses on one line: "
        f"{continued!r}\n{cont.stderr[:400]}"
    )


# Quotes inside the verb: the shell removes them, so `cl''ean` runs `clean`. The
# parser already resolves these correctly; the leak was the raw-text early exit that
# never let the parser see them.
_QUOTED = [
    ("discard", _DISCARD, f"{GIT} cl''ean -fd"),
    ("discard-double", _DISCARD, f'{GIT} c"lea"n -fd'),
    ("protected", _PROTECTED, "r''m -rf ~/genesis/data"),
    ("commit", _COMMIT_GATE, f"{GIT} co''mmit {NV} -m x"),
    ("destructive", _DESTRUCTIVE, "r''m -rf ~"),
]


@pytest.mark.parametrize(
    ("guard", "command"), [(g, c) for _, g, c in _QUOTED], ids=[i for i, _, _ in _QUOTED]
)
def test_a_quote_inside_the_verb_does_not_skip_the_gate(sandbox, guard, command):
    home, repo = sandbox
    res = _run(guard, command, home, repo)
    assert _refused(res), (
        f"{guard.name} allowed {command!r}; bash runs it with the quotes removed.\n"
        f"{res.stderr[:400]}"
    )


def test_a_plain_push_split_inside_the_verb_is_refused_not_run_unasked(sandbox):
    """No force flag, so nothing else in the raw text names a gated operation: the
    word `push` itself is the only mention, and the continuation splits it. The
    blind branch must still see it (through `mentions`), or the command reaches
    the shell with neither a refusal nor the ordinary push approval."""
    home, repo = sandbox
    res = _run(_PUSH_GUARD, f"{GIT} pu{MID}sh origin feature-x", home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


def test_a_continued_review_request_is_refused_not_passed_unbudgeted(sandbox):
    """The push guard's review-request budget reads the SEGMENTS for a
    `gh pr comment … @codex review`. A continuation returns none, so without the
    blind branch naming review requests the command reached GitHub with no budget
    check at all. MEASURED on the corpus differential: a real multi-line reply loop
    that ended in `@codex review` went ASK on main -> ALLOW before this cell existed."""
    home, repo = sandbox
    cmd = f"gh pr comment 1 {CONT}--body '@codex review'"
    res = _run(_PUSH_GUARD, cmd, home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


_WORKTREE_GUARD = _HOOKS_DIR / "worktree_cwd_guard.py"


@pytest.mark.parametrize(
    "command",
    [
        "git worktree" + CONT + "remove {t}",
        "git worktree re" + MID + "move {t}",
        "git work" + MID + "tree remove {t}",
    ],
    ids=["between-words", "inside-verb", "inside-subcommand"],
)
def test_a_continued_worktree_removal_is_refused(sandbox, command):
    """Every direct worktree removal is refused on one line (the lifecycle manager
    owns removal). The continued form must get the same verdict."""
    home, repo = sandbox
    target = repo / "wt"
    target.mkdir()
    one_line = _run(_WORKTREE_GUARD, f"git worktree remove {target}", home, repo)
    assert one_line.returncode == 2, "control: the one-line removal must be refused"
    res = _run(_WORKTREE_GUARD, command.format(t=target), home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


def test_a_continued_removal_inside_a_launcher_is_refused(sandbox):
    """The destructive guard's token scan folds continuations itself, so a direct
    `r<continuation>m -rf ~` is refused with or without the resolver. A removal
    carried inside a launcher is different: the token scan sees only `bash`, and
    only the resolver can refuse it — and the resolver runs only when its prefilter
    finds `rm`, which the raw text does not spell."""
    home, repo = sandbox
    res = _run(_DESTRUCTIVE, f'bash -c "r{MID}m -rf ~"', home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


def test_a_pytest_split_inside_its_name_is_refused(sandbox):
    """full_suite_guard refuses a bounds-type blind spot only when the command
    names pytest; that check read the raw text, where a continuation inside the
    word hides it."""
    home, repo = sandbox
    res = _run(_HOOKS_DIR / "full_suite_guard.py", f"py{MID}test tests/", home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


# ── negative controls: a guard that refused everything would pass every cell above ──

_BENIGN_CONTINUED = [
    "ls" + CONT + "-la",
    "echo hello" + CONT + "world",
    "python3 -c 'print(1)'" + CONT + "&& echo done",
]


@pytest.mark.parametrize("guard", [g for _, g, _ in _GATED], ids=[i for i, _, _ in _GATED])
@pytest.mark.parametrize("command", _BENIGN_CONTINUED)
def test_a_continued_command_naming_no_gated_operation_is_allowed(sandbox, guard, command):
    home, repo = sandbox
    res = _run(guard, command, home, repo)
    assert not _refused(res), (
        f"{guard.name} refused {command!r}, which names nothing it gates. The refusal "
        f"must stay behind each guard's own mention check.\n{res.stderr[:400]}"
    )
    # "Not refused" is also what a CRASH looks like (the degraded exit is 1, which
    # the harness treats as non-blocking), so pin the clean allow itself.
    assert res.returncode == 0, f"{guard.name} exited {res.returncode}.\n{res.stderr[:400]}"


@pytest.mark.parametrize("guard", [g for _, g, _ in _GATED], ids=[i for i, _, _ in _GATED])
def test_the_one_line_benign_command_is_still_allowed(sandbox, guard):
    home, repo = sandbox
    res = _run(guard, "git status", home, repo)
    assert not _refused(res), f"{guard.name} refused `git status`.\n{res.stderr[:400]}"
    assert res.returncode == 0, f"{guard.name} exited {res.returncode}.\n{res.stderr[:400]}"


def test_the_refusal_names_the_continuation_and_the_one_line_remedy(sandbox):
    """A refusal is only a cost, not a wall, if it says what to do. The message must
    name the cause a reader can recognise and a remedy they can perform."""
    home, repo = sandbox
    res = _run(_DISCARD, f"{GIT}{CONT}{CLEAN} -fd", home, repo)
    assert res.returncode == 2
    assert "continu" in res.stderr, res.stderr
    assert "one line" in res.stderr, res.stderr


# ── the parser's report ──


def test_a_continuation_is_a_bounds_type_blind_spot_with_no_segments():
    segs, blind = sp.analyze_checked(f"{GIT}{CONT}{CLEAN} -fd")
    assert blind is sp._BLIND_CONTINUATION
    assert blind.bounds_induced is True
    assert segs == [], "a bounds-type blind spot must return no segments"


def test_the_continuation_cause_is_in_the_blind_spot_domain():
    assert sp._BLIND_CONTINUATION in sp._ALL_BLIND_SPOTS


def test_the_one_line_command_has_no_blind_spot():
    _, blind = sp.analyze_checked(f"{GIT} {CLEAN} -fd")
    assert blind is None


@pytest.mark.parametrize(
    "command",
    [
        f"echo a\n{GIT} status",  # a plain newline
        "echo 'a\\b'",  # a backslash not before a newline
    ],
    ids=["plain-newline", "no-newline"],
)
def test_a_newline_or_backslash_on_its_own_is_not_reported(command):
    assert not sp.has_continuation(command)
    _, blind = sp.analyze_checked(command)
    assert blind is not sp._BLIND_CONTINUATION


@pytest.mark.parametrize(
    "command",
    ["echo a\\\n b", "echo a\\\\\n b", "echo a\\\\\\\n b"],
    ids=["one", "two", "three"],
)
def test_any_run_of_backslashes_before_a_newline_is_reported(command):
    """Counting the run is right only for the command as typed. One layer down — a
    payload handed to another shell, backticks, a heredoc fed to a shell — the
    outer layer halves the run, so an EVEN run becomes a join there. The detector
    therefore does not count: any backslash directly before a newline is reported,
    which over-refuses an even run at the top level and never misses a nested join
    spelled in the text."""
    assert sp.has_continuation(command)


def test_the_detector_tries_each_backslash_run_once():
    """The detector runs on the hook path several times per call, so its regex must
    stay linear in the command's length. Anchoring each attempt at the start of a
    run is what does that (see `_CONTINUATION_NL`); this pins the anchor, and the
    count below pins that a long run is still found. A wall-clock bound would be
    install-dependent, so the bounded ALGORITHM is what is locked."""
    assert sp._CONTINUATION_NL.pattern.startswith(r"(?<!\\)")
    assert sp.has_continuation("x" + "\\" * 50_000 + "\n")
    assert not sp.has_continuation("x" + "\\" * 50_000)


# Joins that happen one layer down: the outer shell turns `\\` into `\` before the
# inner shell reads the line. Each is refused like its one-line form.
_NESTED = [
    ("discard-bash-c", _DISCARD, 'bash -c "git cl' + "\\\\\n" + 'ean -fd"'),
    ("destructive-bash-c", _DESTRUCTIVE, 'bash -c "r' + "\\\\\n" + 'm -rf ~"'),
    ("protected-bash-c", _PROTECTED, 'bash -c "r' + "\\\\\n" + 'm -rf ~/genesis/data"'),
]


@pytest.mark.parametrize(
    ("guard", "command"), [(g, c) for _, g, c in _NESTED], ids=[i for i, _, _ in _NESTED]
)
def test_a_join_one_layer_down_is_refused(sandbox, guard, command):
    home, repo = sandbox
    res = _run(guard, command, home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


# A verb held in a variable: the push guard refuses `git $V …` because the segment's
# operation is unreadable. A continuation withholds the segments, so that fact has to
# survive the withholding.
@pytest.mark.parametrize(
    "command",
    [f"{GIT} $V{CONT}origin main", "gh pr $OP 5" + CONT + "--squash", f"{GIT}{CONT}$V origin main"],
    ids=["git-var-verb", "gh-var-op", "var-verb-after-split"],
)
def test_a_continued_command_with_a_variable_verb_is_refused(sandbox, command):
    home, repo = sandbox
    res = _run(_PUSH_GUARD, command, home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


def test_a_continued_read_only_api_call_with_a_variable_is_not_refused(sandbox):
    """The control for the cell above: an expansion in an ARGUMENT is ordinary work
    (a repository slug in a path). MEASURED, a rule keyed on 'git/gh plus any `$`'
    would have refused 121 recorded commands like this one."""
    home, repo = sandbox
    cmd = 'gh api "repos/$REPO/pulls/1/comments"' + CONT + "--jq ."
    res = _run(_PUSH_GUARD, cmd, home, repo)
    assert res.returncode != 2, (res.stdout + res.stderr)[:400]


# Quotes inside a verb carried by a launcher: the per-segment check that decides
# whether a launcher's payload names the operation read the segment's raw text.
_CARRIED = [
    ("protected-eval", _PROTECTED, "eval 'r\"\"m -rf ~/genesis/data'"),
    ("destructive-eval", _DESTRUCTIVE, "eval 'r\"\"m -rf ~'"),
    ("worktree-eval", _HOOKS_DIR / "worktree_cwd_guard.py", "eval 'git worktree rem\"\"ove {t}'"),
    ("push-eval", _PUSH_GUARD, f"eval '{GIT} pu\"\"sh origin main'"),
    # The push guard's carrier check is two tests joined by OR, and each covers one
    # operation the other does not: `pr create` only the guard's own mention set,
    # `commit` only the carrier net. One cell per arm, so each arm is pinned.
    ("push-eval-create", _PUSH_GUARD, "eval 'gh pr cr\"\"eate --fill'"),
    ("push-eval-commit", _PUSH_GUARD, f"eval '{GIT} com\"\"mit -n -m x'"),
]


@pytest.mark.parametrize(
    ("guard", "command"), [(g, c) for _, g, c in _CARRIED], ids=[i for i, _, _ in _CARRIED]
)
def test_a_quote_split_verb_inside_a_launcher_is_refused(sandbox, guard, command):
    home, repo = sandbox
    target = repo / "wt"
    target.mkdir()
    res = _run(guard, command.format(t=target), home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]


def test_an_unreadable_removal_is_not_told_its_target_is_protected(sandbox):
    """The protected-path guard refuses an rm it cannot read without knowing its
    targets, so its message must not assert what it has not established."""
    home, repo = sandbox
    res = _run(_PROTECTED, f"{RM} /tmp/a{CONT}/tmp/b", home, repo)
    assert res.returncode == 2
    assert "This target holds irreplaceable data" not in res.stderr, res.stderr
    assert "cannot tell whether" in res.stderr, res.stderr


def test_the_close_advisory_still_speaks_for_a_continued_close(sandbox):
    """An advisory, so the cost of a wrong note is a sentence — and silence on a
    genuine close is the failure it exists to prevent."""
    home, repo = sandbox
    res = _run(
        _HOOKS_DIR / "pr_close_advisory.py",
        "gh pr close 5" + CONT + "--comment superseded",
        home,
        repo,
    )
    assert res.returncode == 0
    assert res.stdout.strip(), "the advisory went silent on a continued close"


# ── A withheld parse withholds per-segment facts. The guards refuse on the blind
#    spot itself; the advisories give a short note from the assembled text. Nothing
#    re-parses the join (review found a new defect each round in a version that did).


def test_the_close_advisory_notes_a_close_after_a_backslash_comment(sandbox):
    """A backslash inside a `#` comment does not join, so the close on the next line
    is its own command. The note is read from the text, so it is not lost to how the
    lines join."""
    home, repo = sandbox
    res = _run(
        _HOOKS_DIR / "pr_close_advisory.py",
        "echo ok # a note \\\ngh pr close 5",
        home,
        repo,
    )
    assert res.returncode == 0
    assert "could not check whether it closes" in res.stdout, res.stdout


def test_a_continued_close_gets_the_short_note_and_no_step_count(sandbox):
    """No count is claimed for a command the parse could not read."""
    home, repo = sandbox
    res = _run(
        _HOOKS_DIR / "pr_close_advisory.py",
        "gh pr close 5" + CONT + "--comment superseded",
        home,
        repo,
    )
    assert res.returncode == 0
    assert "could not check whether it closes" in res.stdout, res.stdout
    assert "steps that close" not in res.stdout, res.stdout


def test_a_continued_listing_is_noted_every_time(sandbox):
    """The capped-read note for an unreadable command is per command, not per
    session: once a keyed note was spent, a later continued listing got nothing."""
    home, repo = sandbox
    capped = _HOOKS_DIR / "capped_read_advisory.py"
    for cmd in (
        "gh api repos/o/r/pulls" + CONT + "--jq length",
        "gh pr list" + CONT + "--state open",
    ):
        res = _run(capped, cmd, home, repo)
        assert res.returncode == 0
        assert "could not check the gh read" in res.stdout, (cmd, res.stdout)


def test_a_listing_whose_program_word_is_split_is_still_noted(sandbox):
    """The capped-read early exit reads the text the shell assembles, so a `gh` word
    split by a continuation still reaches the check."""
    home, repo = sandbox
    res = _run(_HOOKS_DIR / "capped_read_advisory.py", "g" + MID + "h pr list", home, repo)
    assert res.returncode == 0
    assert "could not check the gh read" in res.stdout, res.stdout


@pytest.mark.parametrize(
    "command",
    [
        "printf x" + CONT + "&& echo 'git worktree remove'",
        "echo 'git worktree remove is documented' && printf x" + CONT + "y",
    ],
    ids=["mention-after", "mention-before"],
)
def test_a_continued_worktree_mention_is_refused_without_a_guessed_target(sandbox, command):
    """Every direct removal is refused whatever its target, so a continued command
    naming the operation is refused on its cause and remedy. Guessing a target from
    the text lent prose words (even from a later command) as the target; the refusal
    now names none. Refusing a continued mention in prose is the priced cost."""
    home, repo = sandbox
    res = _run(_HOOKS_DIR / "worktree_cwd_guard.py", command, home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]
    assert "cannot tell whether it removes one" in res.stderr, res.stderr
    assert "Direct worktree removal is disabled" not in res.stderr, res.stderr


def test_a_launcher_next_to_a_mention_borrows_no_target_from_another_reading(sandbox):
    """The launcher fallback still reads targets from TEXT, over each mention reading
    separately; read as one joined string, the word after a mention at the end of
    one reading was taken from the start of the next."""
    home, repo = sandbox
    res = _run(
        _HOOKS_DIR / "worktree_cwd_guard.py",
        "eval 'echo x' && echo 'git worktree remove'",
        home,
        repo,
    )
    assert res.returncode == 0, (res.stdout + res.stderr)[:400]


def test_the_tmux_note_survives_a_continuation(sandbox):
    """Advisory-only: the binding cannot be read from a continued command, so the
    advice is given as it stands."""
    home, repo = sandbox
    res = _run(_HOOKS_DIR / "tmux_kill_server_guard.py", "tmux" + CONT + "kill-server", home, repo)
    assert res.returncode == 0
    assert "kill-server" in res.stdout, f"the tmux note went silent: {res.stdout!r}"


def test_the_push_guard_refuses_a_continued_pr_create_with_a_split_verb(sandbox):
    """`pr create` is named only by the guard's own mention set (the carrier net has
    no `create`), so this cell pins that set reading the assembled text on a
    continued command."""
    home, repo = sandbox
    res = _run(_PUSH_GUARD, "gh pr cr''eate --fill" + CONT + "--draft", home, repo)
    assert _refused(res), (res.stdout + res.stderr)[:400]


def test_the_push_guard_refuses_a_continued_hook_skipping_commit(sandbox):
    """On one line the push guard refuses `commit -n` from the segment; a withheld
    parse has no segment, so its blind branch must still name the commit. The commit
    gate refuses the same command too; this pins that the push guard does not quietly
    depend on it."""
    home, repo = sandbox
    one_line = _run(_PUSH_GUARD, f"{GIT} {COMMIT} -n -m x", home, repo)
    assert _refused(one_line), "control: the one-line form must be refused"
    res = _run(_PUSH_GUARD, f"{GIT} {COMMIT} -n" + CONT + "-m x", home, repo)
    assert _refused(res), (res.stdout + res.stderr)[:400]


# ── Refusal messages say only what the guard knows: a continued command whose text
#    merely MENTIONS an operation may not perform it. ──


def test_the_discard_refusal_names_the_word_it_matched(sandbox):
    home, repo = sandbox
    res = _run(_DISCARD, f"{GIT} log" + CONT + f"--grep={RM}", home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]
    assert f"mentions `git` and `{RM}`" in res.stderr, res.stderr
    assert "one line" in res.stderr, res.stderr


def test_the_protected_refusal_does_not_call_it_an_rm_command(sandbox):
    home, repo = sandbox
    res = _run(_PROTECTED, "echo x" + CONT + f"&& {RM} -rf ~/genesis/data", home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]
    assert f"a command that mentions {RM}" in res.stderr, res.stderr
    assert f"an {RM} command that" not in res.stderr, res.stderr


def test_the_clean_refusal_scopes_its_dry_run_promise(sandbox):
    """A continued dry run is refused too, so the message may only promise dry runs
    once the command parses."""
    home, repo = sandbox
    res = _run(_DISCARD, f"{GIT} {CLEAN}" + CONT + "-n", home, repo)
    assert res.returncode == 2, (res.stdout + res.stderr)[:400]
    assert "Once the command parses, its dry-run forms are allowed" in res.stderr, res.stderr


def test_a_bound_still_outranks_a_continuation():
    """Both are bounds-type, so the verdict is the same either way; the ORDER only
    decides which remedy is printed, and the bound's is the one that must be acted
    on first (a continuation fixed on an over-long command is still over-long)."""
    cmd = "echo " + "x" * (sp.MAX_COMMAND_CHARS + 10) + CONT + "done"
    _, blind = sp.analyze_checked(cmd)
    assert blind is sp._BLIND_OVER_LONG


def test_a_continuation_outranks_untokenizable():
    """The discard guard ignores non-bounds causes, so reporting ``untokenizable`` for
    a continued command that is ALSO untokenizable would hand it the one answer it is
    documented to ignore — the same precedence rule the bounds already follow."""
    cmd = f"{GIT}{CONT}{CLEAN} -fd # don" + chr(39) + "t"
    assert sp.untokenizable(cmd), "fixture must genuinely defeat the tokenizer"
    _, blind = sp.analyze_checked(cmd)
    assert blind is sp._BLIND_CONTINUATION


# ── the early-exit view ──


@pytest.mark.parametrize(
    "command",
    [
        f"{GIT} {CLEAN} -fd",
        f"{GIT} cl{MID}ean -fd",
        f"{GIT} cl''ean -fd",
        f'{GIT} c"lea"n -fd',
        f"{GIT} cl\\ean -fd",
        "echo 'unrelated'",
    ],
)
def test_the_first_mention_reading_is_always_the_raw_text(command):
    """Widen-only: a pattern that matched the raw command still matches a reading."""
    assert sp.mention_views(command)[0] == command
    assert sp.mentions(command, command)


@pytest.mark.parametrize(
    "command",
    [f"{GIT} cl{MID}ean -fd", f"{GIT} cl''ean -fd", f'{GIT} c"lea"n -fd', f"{GIT} cl\\ean -fd"],
)
def test_a_mention_reading_shows_the_word_the_shell_runs(command):
    assert sp.mentions(command, f"{GIT} {CLEAN} -fd")


# ── the readings are alternatives, never consecutive text ──


def test_a_pattern_never_matches_across_two_readings():
    """Joined into one string, the last word of one reading sat next to the first word
    of the next, so a two-word pattern matched text no reading contains."""
    command = "alpha" + CONT + "beta omega"
    assert not sp.mentions(command, re.compile(r"\bomega\s+alpha\b"))
    assert sp.mentions(command, re.compile(r"\bbeta\s+omega\b")), "control: in one reading"


def test_every_pattern_must_match_in_the_same_reading():
    """`cl''ean` is in the raw reading only, `clean` in the de-quoted one only: no
    single reading names both, so the conjunction does not hold."""
    command = f"{GIT} cl''ean -fd"
    assert not sp.mentions(command, "cl''ean", CLEAN)
    assert sp.mentions(command, GIT, CLEAN), "control: both in the de-quoted reading"


def test_a_continued_command_ending_in_the_subcommand_is_not_a_removal(sandbox):
    """The removal pattern matched the subcommand ending one reading and the operation
    starting the next, and refused a command that removes nothing."""
    home, repo = sandbox
    res = _run(_WORKTREE_GUARD, "remove" + CONT + "foo git worktree", home, repo)
    assert res.returncode == 0, (res.stdout + res.stderr)[:400]


@pytest.mark.parametrize(
    "command",
    [
        "gh issue comment 1" + CONT + "--body hi",
        "gh api repos/o/r/pulls/1/comments" + CONT + "-f body=thanks",
    ],
    ids=["issue-comment", "review-reply"],
)
def test_a_continued_comment_that_is_not_a_pr_comment_is_not_refused(sandbox, command):
    """The review-request arm names what the parsed check gates, `gh pr comment`; an
    issue comment or an API review reply is neither, and was refused."""
    home, repo = sandbox
    res = _run(_PUSH_GUARD, command, home, repo)
    assert res.returncode == 0, (res.stdout + res.stderr)[:400]


@pytest.mark.parametrize(
    "command",
    [
        "gh --repo o/r pr list" + CONT + "--state open",
        "gh pr ls" + CONT + "--state open",
        "gh api -X GET search/issues" + CONT + "-f q=repo:o/r",
        "gh api repos/o/r/issues" + CONT + "--jq length; gh pr comment 2 -f body=thanks",
    ],
    ids=["option-before-group", "ls-alias", "explicit-get", "field-on-another-command"],
)
def test_a_continued_gh_read_is_noted_however_it_is_spelled(sandbox, command):
    """The note used to depend on a text classifier of gh's grammar, and each spelling
    it did not model got no note. It now needs only `gh` and a read verb."""
    home, repo = sandbox
    res = _run(_HOOKS_DIR / "capped_read_advisory.py", command, home, repo)
    assert res.returncode == 0
    assert "could not check the gh read" in res.stdout, res.stdout


def test_a_continued_gh_command_with_no_read_verb_gets_no_note(sandbox):
    """Control for the cell above: the note is not given to every continued `gh`."""
    home, repo = sandbox
    res = _run(_HOOKS_DIR / "capped_read_advisory.py", "gh pr create" + CONT + "--fill", home, repo)
    assert res.returncode == 0
    assert res.stdout.strip() == "", res.stdout
