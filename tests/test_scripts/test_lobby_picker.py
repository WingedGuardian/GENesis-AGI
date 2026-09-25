"""The fleet picker must never act on a single byte.

WHY THIS FILE EXISTS
--------------------
MEASURED 2026-09-24: the operator's terminal answers a DECRQM mode query with
`ESC [ ? 2004 ; 2 $ y`, and tmux 3.4's client key parser delivers those bytes as
KEYSTROKES -- it special-cases Device-Attributes replies (final byte `c`) and
does not recognise DECRPM replies (final byte `y`). The old landing screen was
`choose-tree`, where `?` opens the search prompt (the "yellow bar") and a bare
digit CHOOSES that entry. One stray reply therefore both painted the status line
and yanked the client into a session, deterministically the same one.

`scripts/lobby-picker.sh` replaces that with a LINE-based menu, so the defence is
structural rather than a timing guess: stray bytes become part of a line, the
line fails to parse, and the menu redraws.

EVERY ABSENCE-ASSERTION HERE IS PAIRED WITH A POSITIVE CONTROL
--------------------------------------------------------------
"no switch happened" is worthless from a harness that could not record a switch
at all, and this session produced exactly that defect elsewhere (four cells
asserting "no alert sent" against a fixture physically unable to send one). So:

  * `test_a_typed_number_switches` proves the fake tmux DOES record
    `switch-client`, which is what makes every "did not switch" assertion below
    mean something;
  * `test_eof_drops_to_a_login_shell` proves the SHELL shim IS reachable, which
    is what makes "did not drop to a shell" mean something.

If either control breaks, treat every other result in this file as void.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PICKER = REPO_ROOT / "scripts" / "lobby-picker.sh"

# The bytes the operator's terminal was measured emitting, verbatim.
DECRPM_REPLY = "\033[?2004;2$y"

_FAKE_TMUX = r"""#!/bin/sh
# Records argv ONE ARGUMENT PER LINE -- `"$*"` joins with single spaces, which
# makes `switch-client -t =my slot` indistinguishable from a two-argument call,
# so a session name containing a space could not be asserted at all.
#
# Deliberately NOT a general tmux: anything unrecognised exits 1 loudly, so a
# picker change that starts calling something new fails here rather than
# silently passing.
{ printf 'ARGV'; for _a in "$@"; do printf '\t%s' "$_a"; done; printf '\n'; } >> "$TMUX_LOG"
case "$1" in
    list-sessions)
        # TMUX_FAIL_LIST_ONCE models a transient server: fail the first call
        # only, so a `continue` that is really an `exit` can be told apart from
        # a real retry.
        if [ -n "${TMUX_FAIL_LIST_ONCE:-}" ] && [ ! -f "$TMUX_LOG.listfailed" ]; then
            : > "$TMUX_LOG.listfailed"
            exit 1
        fi
        [ -n "${TMUX_NO_SERVER:-}" ] && exit 1
        # The picker asks tmux to filter, so the fake must filter: `-f` with the
        # lobby-* match drops the throwaway per-connection sessions SERVER-side.
        # A fake that ignored `-f` would answer a question the picker no longer
        # asks, and every filtering assertion below would be vacuous.
        _rows=${TMUX_ROWS:-"0 cc-1
1 cc-2
0 lobby
0 lobby-999"}
        case " $* " in
            *"m:lobby-*"*)
                printf '%s\n' "$_rows" | while IFS= read -r _r; do
                    case "${_r#* }" in lobby-*) ;; *) printf '%s\n' "$_r" ;; esac
                done
                ;;
            *) printf '%s\n' "$_rows" ;;
        esac
        exit 0
        ;;
    switch-client)
        [ -n "${TMUX_SWITCH_FAILS:-}" ] && exit 1
        exit 0
        ;;
    display-message) printf '%s\n' "${TMUX_IN_MODE:-0}"; exit 0 ;;
    choose-tree)     [ -n "${TMUX_TREE_FAILS:-}" ] && exit 1; exit 0 ;;
    *) echo "fake tmux: unhandled $*" >&2; exit 1 ;;
esac
"""

_FAKE_SHELL = "#!/bin/sh\nprintf 'SHELL_REACHED\\n'\nexit 0\n"


def _run(tmp_path, stdin_bytes: str, **envextra):
    """Drive the real picker with a fake tmux and a fake login shell."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tmux = bin_dir / "tmux"
    tmux.write_text(_FAKE_TMUX)
    tmux.chmod(0o755)
    shell = bin_dir / "fakeshell"
    shell.write_text(_FAKE_SHELL)
    shell.chmod(0o755)
    log = tmp_path / "tmux.log"
    log.write_text("")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TMUX_LOG"] = str(log)
    # The picker ends EOF and `q` with `exec "${SHELL:-/bin/sh}" -l`. Pointing
    # SHELL at a marker script makes that observable instead of spawning a real
    # login shell inside the test runner.
    env["SHELL"] = str(shell)
    env.update({k: str(v) for k, v in envextra.items()})

    proc = subprocess.run(
        ["sh", str(PICKER)],
        input=stdin_bytes,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, log.read_text()


def _switches(tmux_log: str) -> list[str]:
    """Switch calls, as the tab-joined argv the fake recorded."""
    return [
        ln[len("ARGV\t") :]
        for ln in tmux_log.splitlines()
        if ln.startswith("ARGV\tswitch-client")
    ]


def _switch_targets(tmux_log: str) -> list[str]:
    """The session NAME each switch-client asked for, `=` stripped.

    Reads the recorded argv field-wise, so a name containing spaces survives.
    """
    out = []
    for ln in tmux_log.splitlines():
        if not ln.startswith("ARGV\tswitch-client"):
            continue
        parts = ln.split("\t")[1:]
        if "-t" in parts:
            out.append(parts[parts.index("-t") + 1].lstrip("="))
    return out


_ROW_RE = re.compile(r"^\s*(\d+)\)(\s*\*?)\s+(.*?)\s*$")


def _menu_rows(stdout: str) -> list[tuple[int, bool, str]]:
    """(number, attached_marker, name) for each row of the LAST menu drawn.

    The suite used to assert only the tmux argv, never the rendered screen --
    which is how an off-by-one in the numbering passed 18 green tests while
    every pick landed one row off. What the operator READS and what the picker
    DOES have to be tied together, and this is the only thing that ties them.
    """
    last = stdout.rsplit("Genesis Fleet", 1)[-1]
    rows = []
    for line in last.splitlines():
        m = _ROW_RE.match(line)
        if m and m.group(3) and not m.group(3).startswith("attached elsewhere"):
            rows.append((int(m.group(1)), "*" in m.group(2), m.group(3)))
    return rows


# --------------------------------------------------------------------------
# POSITIVE CONTROLS -- these two make every absence-assertion below meaningful.
# --------------------------------------------------------------------------


def test_a_typed_number_switches(tmp_path):
    """CONTROL: the harness can record a switch. Also the feature itself."""
    proc, log = _run(tmp_path, "1\n")
    assert _switch_targets(log) == ["cc-1"], log
    assert proc.returncode == 0
    assert "SHELL_REACHED" not in proc.stdout


def test_eof_drops_to_a_login_shell(tmp_path):
    """CONTROL: the SHELL shim is reachable.

    Also the behaviour itself: EOF must NOT let the pane command exit, which
    would destroy the window, then the session, then detach the client -- i.e.
    log the operator out of a prompt that offered them a shell.
    """
    proc, log = _run(tmp_path, "")
    assert "SHELL_REACHED" in proc.stdout
    assert _switches(log) == [], log


# --------------------------------------------------------------------------
# THE BUG: a stray terminal reply must not choose anything.
# --------------------------------------------------------------------------


def test_a_stray_decrpm_reply_chooses_nothing(tmp_path):
    """The exact bytes the operator's terminal emits, then their Enter.

    Under choose-tree this sequence opened the search prompt AND selected an
    entry. Here it must be an unparseable line.
    """
    # Follow the stray reply with a real selection. Feeding it alone would end
    # the stream, and EOF legitimately drops to a shell -- so "no shell" would
    # have been asserting the wrong thing. This is the stronger property: the
    # stray bytes are swallowed as ONE unparseable line, and the operator's next
    # keystroke still works. (The earlier form was also a dead assertion: its
    # right disjunct was implied by the redraw check below.)
    proc, log = _run(tmp_path, DECRPM_REPLY + "\n1\n")
    assert _switch_targets(log) == ["cc-1"], log
    # The menu redrew rather than acting: once before the stray line, once after.
    assert proc.stdout.count("Genesis Fleet") >= 2, proc.stdout


def test_a_stray_reply_with_no_enter_still_chooses_nothing(tmp_path):
    """The bytes alone, with the stream then ending.

    Models the leak arriving with no operator keypress at all. `read` sees an
    unterminated line at EOF, which must not be treated as a selection.
    """
    proc, log = _run(tmp_path, DECRPM_REPLY)
    assert _switches(log) == [], log


def test_no_single_digit_is_ever_acted_on_without_a_newline(tmp_path):
    """A bare digit was the byte that actually stole the session.

    Every digit, alone, with no Enter: none may switch. The control above
    proves a digit FOLLOWED by Enter does switch, so this is a statement about
    line-termination, not about digits being ignored.
    """
    for d in "0123456789":
        _proc, log = _run(tmp_path, d)
        assert _switches(log) == [], f"digit {d!r} switched: {log}"


# --------------------------------------------------------------------------
# The rest of the menu contract.
# --------------------------------------------------------------------------


def test_bare_enter_redraws_and_does_nothing(tmp_path):
    proc, log = _run(tmp_path, "\n")
    assert _switches(log) == [], log
    assert proc.stdout.count("Genesis Fleet") >= 2, proc.stdout


def test_out_of_range_and_garbage_lines_redraw(tmp_path):
    for line in ("99", "0", "-1", "1 2", "  ", "t t", ";;", "$(id)", "../../etc"):
        proc, log = _run(tmp_path, line + "\n")
        assert _switches(log) == [], f"{line!r} switched: {log}"
        assert proc.stdout.count("Genesis Fleet") >= 2, f"{line!r}: {proc.stdout}"


def test_t_opens_the_tree(tmp_path):
    """choose-tree is not removed -- it stops being the exposed default."""
    _proc, log = _run(tmp_path, "t\n")
    assert any(ln.startswith("ARGV\tchoose-tree") for ln in log.splitlines()), log
    assert _switches(log) == [], log


def test_q_drops_to_a_login_shell(tmp_path):
    """The menu must not redraw after `q`.

    Asserting only that the shell was reached is VACUOUS here, and a mutation
    proved it: turning `q` into `continue` still ends the run at EOF, which
    execs the same shell, so SHELL_REACHED appeared and the test passed against
    a broken `q`. The count is what separates the two paths -- `q` exits on the
    FIRST menu, `continue` draws a second one.
    """
    proc, log = _run(tmp_path, "q\n")
    assert "SHELL_REACHED" in proc.stdout
    assert proc.stdout.count("Genesis Fleet") == 1, proc.stdout
    assert _switches(log) == [], log


def test_the_per_connection_pickers_are_hidden_but_real_sessions_are_not(tmp_path):
    """`lobby-999` is a throwaway door session; `lobby` is the persistent one.

    A filter that dropped both would hide a session the operator uses, and one
    that dropped neither would list a session per open connection.
    """
    proc, _log = _run(tmp_path, "")
    assert "cc-1" in proc.stdout
    assert "cc-2" in proc.stdout
    assert "lobby" in proc.stdout
    assert "lobby-999" not in proc.stdout, proc.stdout


def test_an_unreachable_tmux_is_not_reported_as_an_empty_fleet(tmp_path):
    """"No sessions yet" would tell the operator their fleet died. It has not."""
    proc, _log = _run(tmp_path, "", TMUX_NO_SERVER="1")
    assert "Cannot reach tmux" in proc.stdout
    assert "No sessions yet" not in proc.stdout


def test_a_failed_switch_says_so_instead_of_silently_redrawing(tmp_path):
    """The operator pressed a number; something must explain the non-event."""
    proc, log = _run(tmp_path, "1\n", TMUX_SWITCH_FAILS="1")
    assert _switch_targets(log) == ["cc-1"], log
    assert "is gone" in proc.stdout, proc.stdout


def test_a_name_tmux_has_escaped_renders_verbatim_and_still_selects(tmp_path):
    """The REAL shape of a dangerous-looking session name.

    This test used to feed a raw ESC byte and assert the picker stripped it.
    That property is true and UNREACHABLE: MEASURED on tmux 3.4, tmux
    vis-encodes a session name at creation and at rename, so a stored name can
    never contain a raw byte in \\001-\\037 -- creating a session named with a
    literal ESC stores and lists `esc\\033[2Jevil` as printable characters. The
    old test therefore certified the picker against input its only real caller
    cannot produce, and it was the sole exercise of `_safe`.

    What matters instead is that the ESCAPED form -- which tmux does emit --
    survives the round trip: it renders exactly as tmux wrote it, and selecting
    that row switches to that exact name rather than to some unescaped variant.
    """
    name = r"cc-\033[2Jevil"
    proc, log = _run(tmp_path, "1\n", TMUX_ROWS=f"0 {name}")

    body = proc.stdout.split("Genesis Fleet", 1)[-1]
    assert "\033[2J" not in body, "a raw escape sequence reached the terminal"
    rows = _menu_rows(proc.stdout)
    assert rows and rows[0][2] == name, rows
    assert _switch_targets(log) == [name], log


# --------------------------------------------------------------------------
# Static: the property, not a spelling.
# --------------------------------------------------------------------------


def _code(path: Path) -> str:
    """Source with comment-only lines removed.

    The picker EXPLAINS the bug in its header, so a naive grep for `choose-tree`
    or `?` matches prose. Stripping full-line comments is enough here because
    the assertions below are about statements, not about trailing text.
    """
    return "\n".join(
        ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")
    )


def test_the_picker_never_reads_a_single_character():
    """`read -n1`/`read -k` would reintroduce the whole bug class."""
    code = _code(PICKER)
    for forbidden in ("read -n", "read -N", "read -k", "read -s -n"):
        assert forbidden not in code, f"{forbidden!r} present in {PICKER}"
    assert "read -r" in code


def test_choose_tree_is_reachable_only_from_an_explicit_key():
    """It must not be what the screen lands in."""
    code = _code(PICKER)
    assert code.count("choose-tree") == 1, code
    # The single occurrence sits after the `t | T)` branch opens.
    t_branch = code.index("t | T)")
    assert code.index("choose-tree") > t_branch


def test_the_tree_wait_is_bounded_in_seconds_and_the_bound_is_short():
    """An unbounded wait on `pane_in_mode` is a black screen whose only exit
    detaches the operator.

    Asserting the substring `-lt` was not enough twice over: a mutation raising
    the count from 3600 to 999999999 passed, and the bound it checked was an
    ITERATION count, which bounds nothing when the round trip inside it can
    block. Assert the real thing -- a deadline in seconds, a ceiling on it, and
    a timeout on the probe.
    """
    code = _code(PICKER)
    assert "pane_in_mode" in code
    m = re.search(r"_deadline=\$\(\( \$\(date \+%s\) \+ (\d+) \)\)", code)
    assert m, "the pane_in_mode wait is not bounded by a wall-clock deadline"
    assert int(m.group(1)) <= 300, (
        f"the tree wait may last {m.group(1)}s; a stuck flag should be a blip, "
        "not an outage -- the operator's only escape is to detach"
    )
    assert re.search(r"timeout \d+ tmux display-message", code), (
        "the pane_in_mode probe is not itself bounded, so a wedged server can "
        "block inside the deadline that is supposed to bound it"
    )


def test_the_picker_uses_no_eval_on_terminal_input():
    assert "eval" not in _code(PICKER)


def test_the_picker_creates_no_temp_file():
    """A temp file leaked one per successful connect: the happy path ends in
    SIGHUP (switch-client -> picker session unattached -> destroy-unattached
    kills the pane) and an EXIT trap does NOT run on SIGHUP -- MEASURED, rc=129.
    """
    code = _code(PICKER)
    assert "mktemp" not in code


# --------------------------------------------------------------------------
# THE ROW THE OPERATOR READS MUST BE THE SESSION THEY GET.
#
# Everything above this line asserted the tmux argv and never the rendered
# screen. An adversarial mutation sweep found the gap: moving the counter
# increment one line down relabels every row (1,2,3 -> 0,1,2) so that typing
# `2` lands on the session displayed as `1` -- and all 18 tests stayed green.
# That is the exact "you land somewhere you did not choose" failure this whole
# screen exists to end, with zero test resistance. These cells tie the two
# together.
# --------------------------------------------------------------------------


def test_every_row_number_selects_the_session_printed_on_that_row(tmp_path):
    """The load-bearing cell. Walks EVERY row rather than sampling one.

    Sampling row 1 would not have caught an off-by-one that shifts all rows
    equally; walking them all pins the mapping itself.
    """
    rows = _menu_rows(_run(tmp_path, "")[0].stdout)
    assert len(rows) >= 3, rows
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1)), (
        f"rows are not numbered 1..N: {rows}"
    )
    for number, _attached, name in rows:
        _proc, log = _run(tmp_path, f"{number}\n")
        assert _switch_targets(log) == [name], (
            f"row {number} reads {name!r} but switched to {_switch_targets(log)}"
        )


def test_the_attached_marker_marks_the_attached_session(tmp_path):
    """`*` means "attached elsewhere" -- inverting it survived the old suite.

    The fake reports cc-2 attached and the others not, precisely so this can be
    asserted in both directions; nothing was reading it.
    """
    rows = _menu_rows(_run(tmp_path, "")[0].stdout)
    marked = {name for _n, att, name in rows if att}
    assert marked == {"cc-2"}, rows


def test_a_number_past_the_end_selects_nothing(tmp_path):
    """Deleting the upper-bound check survived the old suite.

    `99` was already covered, but only as garbage. This pins the BOUNDARY: one
    past the last real row, which is where an off-by-one range check fails.
    """
    rows = _menu_rows(_run(tmp_path, "")[0].stdout)
    _proc, log = _run(tmp_path, f"{len(rows) + 1}\n")
    assert _switch_targets(log) == [], log


# --------------------------------------------------------------------------
# A `continue` THAT BECOMES AN `exit` IS A LOGOUT.
#
# Both error branches end in `continue`. Turning either into `exit 0` passed
# the old suite, because the only fixtures reaching them drove EOF -- which
# leaves via the shell on the same line, so the two are indistinguishable.
# In production that exit destroys the window, then the session (the door sets
# destroy-unattached on), then detaches the client.
# --------------------------------------------------------------------------


def test_a_transient_tmux_failure_retries_rather_than_exiting(tmp_path):
    """Fail the FIRST list only. A real retry then selects; an exit cannot."""
    proc, log = _run(tmp_path, "\n1\n", TMUX_FAIL_LIST_ONCE="1")
    assert "Cannot reach tmux" in proc.stdout
    assert _switch_targets(log) == ["cc-1"], (
        "the picker did not recover from a transient list-sessions failure; "
        f"stdout={proc.stdout!r}"
    )


def test_an_all_throwaway_server_offers_a_refresh_rather_than_exiting(tmp_path):
    """Reachable for real: a server whose only session is a per-connection
    picker, i.e. the door's own `lobby` was killed. Verified against real tmux.

    The operator must get the refresh prompt twice -- once, then again after
    Enter -- which an `exit 0` on that branch cannot produce.
    """
    proc, log = _run(tmp_path, "\n\n", TMUX_ROWS="0 lobby-777")
    assert "No sessions yet" in proc.stdout
    assert proc.stdout.count("No sessions yet") >= 2, (
        f"the empty-list branch did not loop; stdout={proc.stdout!r}"
    )
    assert _switches(log) == [], log


# --------------------------------------------------------------------------
# One definition of "a throwaway picker".
# --------------------------------------------------------------------------


def test_a_real_session_whose_name_contains_lobby_stays_visible(tmp_path):
    """The filter must match the NAME, not the rendered line.

    MEASURED on tmux 3.4: the old `grep -v ' lobby-'` matched anywhere in the
    line, so `my lobby-notes` and `cc-9 lobby-x` were hidden from the menu while
    the `t` tree -- which uses tmux's own filter -- still showed them. Sessions
    the operator owns, unreachable by number, with the two halves of one screen
    disagreeing about what exists.
    """
    rows = _menu_rows(
        _run(
            tmp_path,
            "",
            TMUX_ROWS="0 cc-1\n0 my lobby-notes\n0 lobby-999\n0 lobby",
        )[0].stdout
    )
    names = [r[2] for r in rows]
    assert "my lobby-notes" in names, names
    assert "lobby" in names, names
    assert "lobby-999" not in names, names


def test_a_session_name_containing_spaces_selects_correctly(tmp_path):
    """`#{session_attached}` comes FIRST in the format so the split is safe.

    Verified against real tmux too: a session named `my scratch pad` lists as
    one row and selecting it switches to that exact name.
    """
    rows = _menu_rows(_run(tmp_path, "", TMUX_ROWS="0 cc-1\n0 my scratch pad")[0].stdout)
    assert [r[2] for r in rows] == ["cc-1", "my scratch pad"], rows
    _proc, log = _run(tmp_path, "2\n", TMUX_ROWS="0 cc-1\n0 my scratch pad")
    assert _switch_targets(log) == ["my scratch pad"], log


# --------------------------------------------------------------------------
# Remaining keys and the tree's own failure path.
# --------------------------------------------------------------------------


def test_the_uppercase_keys_work_too(tmp_path):
    """`q | Q)` and `t | T)` -- dropping either alternative passed the old suite,
    so uppercase could have silently stopped working."""
    proc, _log = _run(tmp_path, "Q\n")
    assert "SHELL_REACHED" in proc.stdout
    assert proc.stdout.count("Genesis Fleet") == 1, proc.stdout

    _proc, log = _run(tmp_path, "T\n")
    assert any(ln.startswith("ARGV\tchoose-tree") for ln in log.splitlines()), log


def test_a_tree_that_fails_to_open_says_so(tmp_path):
    """The operator pressed `t` and something has to explain the non-event --
    the same principle the failed-switch path already follows."""
    proc, log = _run(tmp_path, "t\n", TMUX_TREE_FAILS="1")
    assert any(ln.startswith("ARGV\tchoose-tree") for ln in log.splitlines()), log
    assert "Could not open the session tree" in proc.stdout, proc.stdout
    assert _switches(log) == [], log
