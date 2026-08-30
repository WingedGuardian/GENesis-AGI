"""The discarded-write note: name what a refusal threw away besides the refused step.

A PreToolUse block discards the WHOLE Bash call. A write chained with a step a
guard refuses is therefore lost SILENTLY — the refusal names only the step it
objected to, so the write reads as having happened.

These tests pin BOTH directions, because a detector that fires on everything
passes every "must fire" case while being worthless:

  * the shapes that really do carry a discarded write are named, including the
    exact command that motivated this;
  * the far larger set of refusals that lost NOTHING stays silent, and each
    scoping decision that keeps it that way is pinned on its own, so widening one
    fails here rather than in a session's ergonomics.

The module is COSMETIC — it may never change a verdict — so the fail-open
properties are asserted directly rather than assumed.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HELPER = _WORKTREE / "scripts" / "hooks" / "discarded_write.py"

sys.path.insert(0, str(_WORKTREE / "scripts" / "hooks"))
_spec = importlib.util.spec_from_file_location("discarded_write", _HELPER)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# The command that motivated all of this: an inline script wrote a file, the
# commit behind it was refused, and the write vanished with no mention.
_MOTIVATING = 'python3 - <<PY\nopen("f","w").write("x")\nPY\ngit commit -m "wip"'


# ── the shapes that DO carry a discarded write ───────────────────────────────


def test_the_motivating_defect_is_named():
    """The whole point. An inline heredoc script ahead of a refused commit."""
    assert _mod.discarded_writes(_MOTIVATING) == ["an inline python3 script"]


def test_a_redirect_target_is_named_exactly():
    """`shell_parse` erases redirect targets from both text views; the note is the
    reason they are now recorded, so this is the load-bearing case."""
    assert _mod.discarded_writes("echo hi > /tmp/f.txt && git commit -m x") == ["/tmp/f.txt"]


def test_an_append_redirect_is_a_write():
    assert _mod.discarded_writes("echo hi >> /tmp/f.log && git push") == ["/tmp/f.log"]


def test_a_heredoc_written_through_cat_is_named():
    """`cat > file <<'EOF'` is the most common write idiom in this repo, and was
    invisible before redirect targets were recorded — bare `cat` looks read-only."""
    cmd = "cat > /tmp/msg.txt <<'EOF'\nsubject\nEOF\ngit commit -F /tmp/msg.txt"
    assert "/tmp/msg.txt" in _mod.discarded_writes(cmd)


def test_a_write_executable_is_named():
    assert _mod.discarded_writes("cp a b && git push") == ["cp a b"]


def test_an_in_place_edit_names_the_COMMAND_not_a_derived_file_list():
    """The note shows the command, not which of its operands are files.

    Deciding that means knowing which options consume a value — unbounded, and it
    shipped two defects across two review rounds: first the in-place SPELLINGS were
    missed, then the fix reported `-e`'s VALUE as an edited file. The raw text is
    correct under any flag, including ones that do not exist yet.
    """
    assert _mod.discarded_writes("sed -i s/a/b/ f.py && git commit -m x") == ["sed -i s/a/b/ f.py"]


@pytest.mark.parametrize(
    "cmd",
    [
        "sed -i s/a/b/ f.py",
        "sed -i.bak s/a/b/ f.py",
        "sed -ibak s/a/b/ f.py",  # suffix attaches with NO separator
        "sed -Ei s/a/b/ f.py",  # clustered short options
        "sed --in-place s/a/b/ f.py",
        "sed --in-place=.bak s/a/b/ f.py",
        "perl -pi -e s/a/b/ f.py",  # perl's clustered form
    ],
)
def test_every_in_place_spelling_is_recognised(cmd):
    """GNU sed documents `-i[SUFFIX], --in-place[=SUFFIX]`, and the suffix attaches
    with no separator. Recognising only `-i` and `-i.` missed the long forms, the
    separator-less suffix, and every CLUSTER — so an ordinary `sed --in-place`
    before a refused push reported that nothing was lost."""
    assert _mod.discarded_writes(f"{cmd} && git push"), f"missed an in-place form: {cmd}"


def test_an_in_place_PROGRAM_is_never_reported_as_an_edited_file():
    """The defect the redesign removes, in both spellings a program can arrive in.

    An earlier revision listed `-e`'s expression and `-f`'s script FILE among the
    edited files — and a test in this suite asserted that second one as correct,
    which is how it survived a review round. Showing the command cannot get this
    wrong, so the assertion is that no operand is singled out at all.
    """
    for cmd in ("sed -i -e s/a/b/ f.py", "sed -i -f prog.sed f.py"):
        (phrase,) = _mod.discarded_writes(f"{cmd} && git push")
        assert phrase == cmd, "the note must be the command, not a parsed file list"
        assert " on " not in phrase, "no operand may be presented as the edited file"


def test_a_quoted_in_place_LOOKALIKE_is_not_a_write():
    """`--` ends option scanning, so an argument that merely contains `-i` is data."""
    assert _mod.discarded_writes("sed -- s/-i/x/ f.py && git push") == []


def test_a_write_AFTER_the_refused_step_is_still_named():
    """Ordering is irrelevant: the whole call is discarded, so a write behind the
    refused step is lost exactly as thoroughly as one in front of it."""
    assert _mod.discarded_writes("git push && cp a b") == ["cp a b"]


# ── the far larger set that lost NOTHING ─────────────────────────────────────


def test_a_single_step_is_silent():
    """The refused step IS the entire call — there is no collateral to report."""
    assert _mod.discarded_writes("git push") == []


def test_a_single_step_that_WRITES_is_still_silent():
    """The suppression has to hold for a lone step that writes, which is the only
    case that distinguishes it — a lone `git push` writes nothing, so it stays
    silent either way and pins nothing. (Mutation-found: the obvious test passed
    against a build with the suppression removed.)"""
    assert _mod.discarded_writes("echo x > /tmp/f") == []
    assert _mod.discarded_writes("cp a b") == []


def test_a_compound_with_no_write_is_silent():
    assert _mod.discarded_writes("cd /x && git push") == []
    assert _mod.discarded_writes("cd /x && pytest tests/t.py") == []


def test_dev_null_is_not_a_lost_write():
    """`2>/dev/null` opens a sink that holds nothing. Reporting it as lost data
    would fire on a large share of ordinary commands."""
    assert _mod.discarded_writes("cat f 2>/dev/null && git push") == []


@pytest.mark.parametrize("sink", ['"/dev/null"', "'/dev/null'", "/dev/null"])
def test_a_QUOTED_null_sink_is_still_not_a_write(sink):
    """bash accepts a quoted redirect target and resolves it to the same file, so
    `> "/dev/null"` is the same sink as `> /dev/null`. Comparing the raw span
    reported the quoted form as a discarded write."""
    assert _mod.discarded_writes(f"printf x > {sink} && git push") == []


def test_a_QUOTED_real_path_is_still_named():
    """The unquoting must not swallow ordinary quoted paths, which is how a path
    containing a space is written in the first place."""
    (phrase,) = _mod.discarded_writes('cp a b > "/tmp/out file.txt" && git push')
    assert "/tmp/out file.txt" in phrase


def test_a_redirect_does_not_MASK_the_command_side_write():
    """`cp a b 2>err.log` writes BOTH. An early return on the redirect target
    reported only `err.log` and stayed silent about the copy — hiding the more
    important write exactly when stderr was redirected, which is common."""
    (phrase,) = _mod.discarded_writes("cp a b 2>err.log && git push")
    assert "err.log" in phrase, "the redirect target must still be named"
    assert "cp a b" in phrase, "the command-side write must not be masked by it"


def test_a_write_ONLY_redirect_segment_is_not_dropped():
    """`> /tmp/result` is a redirection with no command, and bash still creates the
    file — VERIFIED with `bash -c '> wo.txt'`. The segment's raw text is empty once
    the redirect is consumed, so filtering segments on raw alone discarded the write
    silently."""
    assert _mod.discarded_writes("> /tmp/result && git push") == ["/tmp/result"]


def test_a_QUOTED_fd_digit_is_still_a_duplication():
    """The same normalization is what lets `>&"1"` read as the dup it is."""
    assert _mod.discarded_writes('make 2>&"1" && git push') == []


def test_an_fd_duplication_is_not_a_write():
    """`2>&1` points a descriptor at another descriptor; it opens no file."""
    assert _mod.discarded_writes("make 2>&1 && git push") == []


def test_an_input_redirect_is_not_a_write():
    assert _mod.discarded_writes("diff < a.txt && git push") == []


def test_fd_duplication_to_a_DIGIT_target_is_not_a_write():
    """`>&1` / `2>&1` / `>&-` duplicate or close a descriptor; they open no file."""
    assert _mod.discarded_writes("echo x >&2 && git push") == []
    assert _mod.discarded_writes("make 2>&1 && git push") == []
    assert _mod.discarded_writes("echo x >&- && git push") == []


def test_a_redirect_to_a_NON_digit_target_after_ampersand_IS_a_write():
    """`>&word` for a non-numeric word OPENS that file, exactly as `&>word` does.

    VERIFIED against bash: `bash -c 'echo hi >&f.log'` creates f.log containing
    "hi". An earlier version of this file asserted the OPPOSITE — it was written
    to kill a mutant, and pinned incorrect shell semantics while doing so, which
    would have made a genuine silent data loss report "nothing was discarded"."""
    assert _mod.discarded_writes("echo x >&err.log && git push") == ["err.log"]
    assert _mod.discarded_writes("echo x 1>&err.log && git push") == ["err.log"]


def test_a_DIGIT_filename_after_a_plain_redirect_is_still_a_write():
    """`echo x > 1` writes a file named `1`. Digits are only special after `>&`,
    so excluding them globally would lose this."""
    assert _mod.discarded_writes("echo x > 1 && git push") == ["1"]


def test_a_heredoc_feeding_the_REFUSED_step_is_silent():
    """`git commit -F - <<MSG` loses a commit message, not a file. The note claims
    writes that hit the filesystem, and must stay truthful about that."""
    assert _mod.discarded_writes("git commit -F - <<MSG\nsubject\nMSG") == []


def test_an_interpreter_running_a_FILE_is_not_an_inline_script():
    """`python3 script.py` re-runs for free — the file still exists. Only inline
    code (`-`, `-c`, `-e`) is unrecoverable once the call is discarded."""
    assert _mod.discarded_writes("python3 script.py && git push") == []


def test_a_redirect_mentioned_inside_quotes_is_not_a_write():
    """Quote-awareness comes from the shared parser; a raw scan for `>` would fire
    on every commit message containing one."""
    assert _mod.discarded_writes('git commit -m "use a > b" && git push') == []


# ── fail-open properties: this must never change a verdict ───────────────────


def test_an_unparseable_command_is_silent_not_an_exception():
    assert _mod.discarded_writes("cmd 'unterminated && git commit -m x") == []


def test_empty_and_whitespace_commands_are_silent():
    assert _mod.discarded_writes("") == []
    assert _mod.discarded_writes("   \n  ") == []


@pytest.mark.parametrize(
    "hostile",
    [
        "\x00\x01\x02",
        ">" * 5000,
        "a" * 20000,
        "cat > " + "x" * 5000 + " && git push",
        "$(`\\'\"" * 200,
    ],
)
def test_hostile_input_never_raises(hostile):
    """A cosmetic helper that raised would take its guard's exit code with it."""
    assert isinstance(_mod.discarded_writes(hostile), list)


def test_a_heredoc_body_phantom_is_not_displayed():
    """`shell_parse` does not understand heredocs: body lines are parsed as if they
    were commands, so a `>` in ordinary prose yields a phantom redirect whose
    target is a run of body text. MEASURED on the block corpus: one such phantom
    was a 500-character blob spanning newlines. Dumping that into a refusal is
    worse than saying nothing."""
    cmd = "python3 - <<PY\ntext = 'a > " + "b" * 400 + "'\nPY\ngit commit -m x"
    for phrase in _mod.discarded_writes(cmd):
        assert len(phrase) <= 200, f"phantom blob leaked into the note: {phrase[:80]!r}"
        assert "\n" not in phrase


def test_an_overlong_target_is_DROPPED_not_truncated():
    """It has to disappear, not be shortened. A truncated 400-character blob still
    reads as a filename and is still wrong; only dropping it is honest. Asserting a
    length bound instead would pass against a build with no cap at all, because the
    per-entry display truncation shortens it anyway. (Mutation-found.)"""
    assert _mod.discarded_writes("cat > " + "x" * 200 + " && git push") == []


def test_a_target_spanning_a_NEWLINE_is_dropped():
    """Reachable via an expansion target: `> "$(echo a\\nb)"` really does yield a
    multi-line target, and that is the shape of the 500-character blob measured on
    the block corpus."""
    assert _mod.discarded_writes('cat > "$(echo a\nb)" && git push') == []


def test_an_absurdly_nested_command_is_bounded_not_parsed():
    """`analyze` is superlinear in nested `$(…)` — MEASURED 2.9s at 800 levels.
    That cost would be paid on a BLOCK path, and a hook killed at its timeout has
    not reached its `exit 2`, which CC reads as non-blocking: the refused command
    runs. A cosmetic note must not be able to buy that."""
    import time

    cmd = ("$(" * 4000) + (")" * 4000) + " > f && git push"
    start = time.monotonic()
    assert _mod.discarded_writes(cmd) == []
    assert time.monotonic() - start < 1.0, "bound must be an O(n) scan, not a parse"


def test_an_absurdly_long_command_is_bounded_not_parsed():
    cmd = "cp " + ("x" * 200_000) + " /dest && git push"
    start = __import__("time").monotonic()
    assert _mod.discarded_writes(cmd) == []
    assert __import__("time").monotonic() - start < 1.0


def test_the_bounds_sit_above_every_real_command_observed():
    """Sized from the corpus this was measured on: longest command 14,682 chars,
    most substitutions in one command 150. A bound that clipped ordinary work
    would make the note silently useless exactly when it is wanted."""
    assert _mod._MAX_COMMAND_CHARS > 14_682
    assert _mod._MAX_SUBSTITUTIONS > 150
    ordinary = "cp a b && echo $(date) $(hostname) > /tmp/f && git push"
    assert _mod.discarded_writes(ordinary), "a normal command must still be read"


def test_every_note_stays_short_enough_to_read():
    """A refusal message people stop reading is a refusal message that does not work."""
    cmd = "cp " + " ".join(f"/tmp/file{i}.txt" for i in range(50)) + " /dest && git push"
    assert len(_mod.note(cmd) or "") < 800


# ── the rendered note and the two entry points ───────────────────────────────


def test_the_note_says_the_WHOLE_command_was_discarded():
    """The misreading this exists to correct is 'only the refused step failed'."""
    text = _mod.note(_MOTIVATING)
    assert "ENTIRE command was discarded" in text
    assert "did NOT happen" in text
    assert "an inline python3 script" in text


def test_the_rerun_guidance_does_not_strip_a_write_of_its_CONDITION():
    """A note that induces the WRONG action is worse than no note.

    `test -f approved && cp draft final && git push` names `cp draft final`. Told
    to "re-run them as their own command", a reader performs the copy even when
    `approved` is absent — which the original shell would never have done. The
    guidance must carry the prerequisite, not detach it."""
    text = _mod.note("test -f approved && cp draft final && git push")
    assert "cp draft final" in text
    lowered = text.lower()
    assert "keeping" in lowered and "guarded" in lowered, (
        "the rerun guidance must tell the reader to preserve what guarded the write"
    )
    assert "as their own command" not in lowered, (
        "the detached-rerun phrasing is what made this unsafe for a conditional write"
    )


def test_no_note_when_nothing_was_discarded():
    assert _mod.note("cd /x && git push") is None


def test_warn_prints_to_stderr(capsys):
    _mod.warn(_MOTIVATING)
    assert "did NOT happen" in capsys.readouterr().err


def test_warn_is_silent_when_there_is_nothing_to_say(capsys):
    _mod.warn("cd /x && git push")
    assert capsys.readouterr().err == ""


def test_remember_then_warn_with_no_argument(capsys):
    """Guards hand over the command where they already extract it, because the
    payload read CONSUMES stdin and cannot be repeated further down."""
    _mod.remember(_MOTIVATING)
    _mod.warn()
    assert "did NOT happen" in capsys.readouterr().err


def test_remember_ignores_junk(capsys):
    _mod.remember(None)
    _mod.remember("")
    _mod.warn("cd /x && git push")
    assert capsys.readouterr().err == ""


# ── the CLI, which is how the shell hooks reach this same implementation ─────


def _cli(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_HELPER), "--command", command],
        capture_output=True,
        text=True,
    )


def test_cli_prints_the_note_to_stderr():
    res = _cli(_MOTIVATING)
    assert "did NOT happen" in res.stderr


def test_cli_always_exits_zero_so_it_cannot_perturb_the_caller():
    """The shell hook's OWN exit code is the verdict. If this CLI could exit
    non-zero it would be able to turn a block into an allow under `set -e`."""
    assert _cli(_MOTIVATING).returncode == 0
    assert _cli("cd /x && git push").returncode == 0
    assert _cli("").returncode == 0


def test_cli_is_silent_when_nothing_was_discarded():
    assert _cli("cd /x && git push").stderr == ""


def test_cli_writes_nothing_to_stdout():
    """Shell hooks may capture stdout; the note belongs on stderr only."""
    assert _cli(_MOTIVATING).stdout == ""
